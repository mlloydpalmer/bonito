"""
Bonito Input/Output
"""

import os
import sys
from glob import glob
from warnings import warn
from logging import getLogger
from os.path import realpath, splitext
from multiprocessing import Process, Queue, Lock, cpu_count

import numpy as np
from tqdm import tqdm
from mappy import Aligner, revcomp

import bonito
from bonito.util import get_raw_data, mean_qscore_from_qstring


logger = getLogger('bonito')


def summary_file():
    """
    Return the filename to use for the summary tsv.
    """
    if sys.stdout.isatty():
        return 'summary.tsv'
    return '%s_summary.tsv' % splitext(realpath('/dev/fd/1'))[0]


def write_summary_header(fd, sep='\t'):
    """
    Write the summary tsv header.
    """
    fd.write('%s\n' % sep.join((
        'filename',
        'read_id',
        'run_id',
        'channel',
        'mux',
        'start_time',
        'duration',
        'template_start',
        'template_duration',
        'sequence_length_template',
        'mean_qscore_template',
    )))
    fd.flush()


def write_summary_row(fd, read, seqlen, qscore=0, sep='\t'):
    """
    Write a summary tsv row.
    """
    fd.write('%s\n' % sep.join(map(str, [
        read.filename,
        read.read_id,
        read.run_id,
        read.channel,
        read.mux,
        read.start,
        read.duration,
        read.template_start,
        read.template_duration,
        seqlen,
        qscore,
    ])))
    fd.flush()


def write_fasta(header, sequence, fd=sys.stdout):
    """
    Write a fasta record to a file descriptor.
    """
    fd.write(">%s\n" % header)
    fd.write("%s\n" % sequence)
    fd.flush()


def write_fastq(header, sequence, qstring, fd=sys.stdout):
    """
    Write a fastq record to a file descriptor.
    """
    fd.write("@%s\n" % header)
    fd.write("%s\n" % sequence)
    fd.write("+\n")
    fd.write("%s\n" % qstring)
    fd.flush()


def write_sam_header(aligner, fd=sys.stdout, sep='\t'):
    """
    Write the SQ & PG sam headers to a file descriptor.
    """
    fd.write('%s\n' % os.linesep.join([
        sep.join([
            '@SQ', 'SN:%s' % name, 'LN:%s' % len(aligner.seq(name))
        ]) for name in aligner.seq_names
     ]))

    fd.write('%s\n' % sep.join([
        '@PG',
        'ID:bonito',
        'PN:bonito',
        'VN:%s' % bonito.__version__,
        'CL:%s' % ' '.join(sys.argv),
    ]))
    fd.flush()


def write_sam(read_id, sequence, mapping, fd=sys.stdout, sep='\t'):
    """
    Write a sam record to a file descriptor.
    """
    softclip = [
        '%sS' % mapping.q_st if mapping.q_st else '',
        mapping.cigar_str,
        '%sS' % (len(sequence) - mapping.q_en) if len(sequence) - mapping.q_en else ''
    ]

    fd.write("%s\n" % sep.join(map(str, [
        read_id,
        0 if mapping.strand == +1 else 16,
        mapping.ctg,
        mapping.r_st + 1,
        mapping.mapq,
        ''.join(softclip if mapping.strand == +1 else softclip[::-1]),
        '*',
        0,
        0,
        sequence if mapping.strand == +1 else revcomp(sequence),
        '*',
        'NM:i:%s' % mapping.NM,
    ])))
    fd.flush()


class PreprocessReader(Process):
    """
    Reader Processor that reads and processes fast5 files
    """
    def __init__(self, directory, maxsize=5):
        super().__init__()
        self.directory = directory
        self.queue = Queue(maxsize)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def run(self):
        for fast5 in tqdm(glob("%s/*fast5" % self.directory), ascii=True, ncols=100):
            for read in get_raw_data(fast5):
                self.queue.put(read)
        self.queue.put(None)

    def stop(self):
        self.join()


class DecoderWriterPool:
   """
   Simple pool of decoder writers
   """
   def __init__(self, model, procs=4, reference=None, **kwargs):
       self.lock = Lock()
       self.queue = Queue()
       self.procs = procs if procs else cpu_count()
       self.decoders = []

       if reference:
           sys.stderr.write("> loading reference\n")
           aligner = Aligner(reference, preset='ont-map')
           if not aligner:
               sys.stderr.write("> failed to load/build index\n")
               sys.exit(1)
           write_sam_header(aligner)
       else:
           aligner = None

       with open(summary_file(), 'w') as summary:
           write_summary_header(summary)

       for _ in range(self.procs):
           decoder = DecoderWriter(model, self.queue, self.lock, aligner=aligner, **kwargs)
           decoder.start()
           self.decoders.append(decoder)

   def stop(self):
       for decoder in self.decoders: self.queue.put(None)
       for decoder in self.decoders: decoder.join()

   def __enter__(self):
       return self

   def __exit__(self, exc_type, exc_val, exc_tb):
       self.stop()


class DecoderWriter(Process):
    """
    Decoder Process that writes fasta records to stdout
    """
    def __init__(self, model, queue, lock, fastq=False, beamsize=5, aligner=None):
        super().__init__()
        self.queue = queue
        self.lock = lock
        self.model = model
        self.fastq = fastq
        self.aligner = aligner
        self.beamsize = beamsize

    def run(self):
        while True:
            job = self.queue.get()
            if job is None: return
            read, predictions = job

            # convert logprobs to probs
            predictions = np.exp(predictions.astype(np.float32))

            sequence, path = self.model.decode(
                predictions, beamsize=self.beamsize, qscores=True, return_path=True
            )
            mean_qscore = mean_qscore_from_qstring(sequence[len(path):])

            if not self.fastq:  # beam search
                sequence, path = self.model.decode(
                    predictions, beamsize=self.beamsize, qscores=False, return_path=True
                )

            if sequence:
                with self.lock, open(summary_file(), 'a') as summary:
                    if self.aligner:
                        for mapping in self.aligner.map(sequence):
                            write_sam(read.read_id, sequence, mapping)
                            break
                    elif self.fastq:
                        write_fastq(read.read_id, sequence[:len(path)], sequence[len(path):])
                    else:
                        write_fasta(read.read_id, sequence)
                    write_summary_row(summary, read, len(sequence), mean_qscore)
            else:
                logger.warn("> skipping empty sequence %s", read.read_id)
