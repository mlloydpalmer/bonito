"""
Bonito Input/Output
"""

import os
import sys
import csv
import pandas as pd
from warnings import warn
from threading import Thread
from logging import getLogger
from contextlib import contextmanager
from os.path import realpath, splitext, dirname
from functools import cached_property

import numpy as np
from mappy import revcomp
from ont_fast5_api.fast5_file import Fast5File
from ont_fast5_api.fast5_info import ReadInfo
import torch
import seqdist

import bonito
from bonito.cli.convert import typical_indices


logger = getLogger('bonito')


class CSVLogger:
    def __init__(self, filename, sep=','):
        self.filename = str(filename)
        if os.path.exists(self.filename):
            with open(self.filename) as f:
                self.columns = csv.DictReader(f).fieldnames
        else:
            self.columns = None
        self.fh = open(self.filename, 'a', newline='')
        self.csvwriter = csv.writer(self.fh, delimiter=sep)
        self.count = 0

    def set_columns(self, columns):
        if self.columns:
            raise Exception('Columns already set')
        self.columns = list(columns)
        self.csvwriter.writerow(self.columns)

    def append(self, row):
        if self.columns is None:
            self.set_columns(row.keys())
        self.csvwriter.writerow([row.get(k, '-') for k in self.columns])
        self.count += 1
        if self.count > 100:
            self.count = 0
            self.fh.flush()

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@contextmanager
def devnull(*args, **kwds):
    """
    A context manager that sends all out stdout & stderr to devnull.
    """
    save_fds = [os.dup(1), os.dup(2)]
    null_fds = [os.open(os.devnull, os.O_RDWR) for _ in range(2)]
    os.dup2(null_fds[0], 1)
    os.dup2(null_fds[1], 2)
    try:
        yield
    finally:
        os.dup2(save_fds[0], 1)
        os.dup2(save_fds[1], 2)
        for fd in null_fds + save_fds: os.close(fd)


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


def write_sam(read_id, sequence, qstring, mapping, fd=sys.stdout, unaligned=False, sep='\t'):
    """
    Write a sam record to a file descriptor.
    """
    if unaligned:
        fd.write("%s\n" % sep.join(map(str, [
            read_id, 4, '*', 0, 0, '*', '*', 0, 0, sequence, qstring, 'NM:i:0'
        ])))
    else:
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
            '*', 0, 0,
            sequence if mapping.strand == +1 else revcomp(sequence),
            qstring,
            'NM:i:%s' % mapping.NM,
            'MD:Z:%s' % mapping.MD,
        ])))
    fd.flush()


def rectify(ragged_array, max_len=None):
    lengths = np.array([len(x) for x in ragged_array], dtype=np.uint16)
    padded = np.zeros((len(ragged_array), max_len or np.max(lengths)), dtype=ragged_array[0].dtype)
    for x, y in zip(ragged_array, padded):
        y[:len(x)] = x
    return padded, lengths


def get_ctc_targets(seqs, dtype=np.int):
    t = bytes.maketrans(b'ACGT', b'\x01\x02\x03\x04')
    targets, target_lengths = rectify([np.array(bytearray(seq, 'utf-8').translate(t)) for seq in seqs])
    return targets.astype(dtype), target_lengths.astype(dtype)


def compute_alignments(model, mapped_read):
    with torch.no_grad():
        device = next(model.parameters()).device
        batch = torch.from_numpy(mapped_read.read.signal).unsqueeze(0).unsqueeze(0)
        scores = model.encoder(batch.to(dtype=torch.float16, device=device))

    targets, target_lengths = [torch.tensor(x, device=device) for x in get_ctc_targets([mapped_read.seq])]
    stay_scores, move_scores = model.seqdist.prepare_ctc_scores(scores, targets)
    target_lengths = target_lengths + 1 - model.seqdist.state_len
    alignments = seqdist.ctc_simple.viterbi_alignments(stay_scores, move_scores, target_lengths)
    return alignments.argmax(2).to(torch.int16).T[0].to('cpu').numpy()


class MappedRead:

    def __init__(self, read, mapping, seq):
        self.read = read
        self.mapping = mapping
        self.seq = seq

    def alignments(self, model):
        return compute_alignments(model, self)


def write_trimmed_fast5(trimmed_fast5s_dir, read, mapping, model, seq):
    if mapping:
        output_file = f"{trimmed_fast5s_dir}/{read.read_id}_trimmed.fast5"
        if os.path.exists(output_file):
            os.remove(output_file)

        mapped_read = MappedRead(read, mapping, seq)
        alignments = mapped_read.alignments(model)

        with Fast5File(str(output_file), 'w') as output_fast5:
            read_attrs = read.read_attrs
            new_read_id = read.read_id + '_trimmed'
            read_attrs['read_id'] = new_read_id

            output_fast5.add_channel_info(read.channel_info)
            output_fast5.set_tracking_id(read.tracking_id)
            output_fast5.add_context_tags(read.context_tags)

            start = np.where(alignments == mapping.q_st)[0].min()
            if len(np.where(alignments == mapping.q_en)[0]) == 0:
                end = len(alignments)
            else:
                end = np.where(alignments == mapping.q_en)[0].max()
            trim_start = start * model.stride
            trim_end = end * model.stride
            trimmed_raw = read.raw[trim_start:trim_end]

            read_attrs['duration'] = len(trimmed_raw)
            read_info = ReadInfo(read_attrs['read_number'], read_attrs['read_id'], read_attrs['start_time'],
                read_attrs['duration'], mux=read_attrs['start_mux'], median_before=read_attrs['median_before'])
            output_fast5.status.read_info.append(read_info)
            n = len(output_fast5.status.read_info) - 1
            output_fast5.status.read_number_map[read_attrs['read_number']] = n
            output_fast5.status.read_id_map[read_attrs['read_id']] = n
            group_name = output_fast5.raw_dataset_group_name
            output_fast5._add_group(group_name, read_attrs)
            output_fast5.add_raw_data(trimmed_raw, attrs=read_attrs)


def get_ref(mapping, aligner):
    ref = aligner.seq(mapping.ctg, mapping.r_st, mapping.r_en)
    return revcomp(ref) if (mapping.strand == -1) else ref


dir_dict = {1: '+', -1: '-'}


def write_ref(refs_file, read_id, mapping, aligner):
    if mapping:
        direction = dir_dict[mapping.strand]
        refseq = get_ref(mapping, aligner)
        refs_file.write(f'>{read_id} {mapping.ctg}:{mapping.r_st}-{mapping.r_en}({direction})\n{refseq}\n')


def summary_file():
    """
    Return the filename to use for the summary tsv.
    """
    stdout = realpath('/dev/fd/1')
    if sys.stdout.isatty() or stdout.startswith('/proc'):
        return 'summary.tsv'
    return '%s_summary.tsv' % splitext(stdout)[0]


def trim_outfiles():
    """
    Return the filenames to use for the trimmed_fast5s and refs.fasta.
    """
    stdout = realpath('/dev/fd/1')
    if sys.stdout.isatty() or stdout.startswith('/proc'):
        return 'trimmed_fast5s', 'refs.fasta'
    outdir = dirname(splitext(stdout)[0])
    return '%s/trimmed_fast5s' % outdir, '%s/refs.fasta' % outdir


summary_field_names = [
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
    #if alignment
    'alignment_genome',
    'alignment_genome_start',
    'alignment_genome_end',
    'alignment_strand_start',
    'alignment_strand_end',
    'alignment_direction',
    'alignment_length',
    'alignment_num_aligned',
    'alignment_num_correct',
    'alignment_num_insertions',
    'alignment_num_deletions',
    'alignment_num_substitutions',
    'alignment_mapq',
    'alignment_strand_coverage',
    'alignment_identity',
    'alignment_accuracy',
]


def summary_row(read, seqlen, qscore, alignment=False):
    """
    Summary tsv row.
    """
    fields = [
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
    ]

    if alignment:

        ins = sum(count for count, op in alignment.cigar if op == 1)
        dels = sum(count for count, op in alignment.cigar if op == 2)
        subs = alignment.NM - ins - dels
        length = alignment.blen
        matches = length - ins - dels
        correct = alignment.mlen

        fields.extend([
            alignment.ctg,
            alignment.r_st,
            alignment.r_en,
            alignment.q_st if alignment.strand == +1 else seqlen - alignment.q_en,
            alignment.q_en if alignment.strand == +1 else seqlen - alignment.q_st,
            '+' if alignment.strand == +1 else '-',
            length, matches, correct,
            ins, dels, subs,
            alignment.mapq,
            (alignment.q_en - alignment.q_st) / seqlen,
            correct / matches,
            correct / length,
        ])

    elif alignment is None:
        fields.extend(
            ['*', -1, -1, -1, -1, '*', 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0]
        )

    return dict(zip(summary_field_names, fields))


duplex_summary_field_names = [
    'filename_template',
    'read_id_template',
    'filename_complement',
    'read_id_complement',
    'run_id',
    'channel_template',
    'mux_template',
    'channel_complement',
    'mux_complement',
    'sequence_length_duplex',
    'mean_qscore_duplex',
    #if alignment
    'alignment_genome',
    'alignment_genome_start',
    'alignment_genome_end',
    'alignment_strand_start',
    'alignment_strand_end',
    'alignment_direction',
    'alignment_length',
    'alignment_num_aligned',
    'alignment_num_correct',
    'alignment_num_insertions',
    'alignment_num_deletions',
    'alignment_num_substitutions',
    'alignment_mapq',
    'alignment_strand_coverage',
    'alignment_identity',
    'alignment_accuracy',
]


def duplex_summary_row(read_temp, comp_read, seqlen, qscore, alignment=False):
    """
    Duplex summary tsv row.
    """
    fields = [
        read_temp.filename,
        read_temp.read_id,
        comp_read.filename,
        comp_read.read_id,
        read_temp.run_id,
        read_temp.channel,
        read_temp.mux,
        comp_read.channel,
        comp_read.mux,
        seqlen,
        qscore,
    ]

    if alignment:

        ins = sum(count for count, op in alignment.cigar if op == 1)
        dels = sum(count for count, op in alignment.cigar if op == 2)
        subs = alignment.NM - ins - dels
        length = alignment.blen
        matches = length - ins - dels
        correct = alignment.mlen

        fields.extend([
            alignment.ctg,
            alignment.r_st,
            alignment.r_en,
            alignment.q_st if alignment.strand == +1 else seqlen - alignment.q_en,
            alignment.q_en if alignment.strand == +1 else seqlen - alignment.q_st,
            '+' if alignment.strand == +1 else '-',
            length, matches, correct,
            ins, dels, subs,
            alignment.mapq,
            (alignment.q_en - alignment.q_st) / seqlen,
            correct / matches,
            correct / length,
        ])

    elif alignment is None:
        fields.extend(
            ['*', -1, -1, -1, -1, '*', 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0]
        )

    return dict(zip(duplex_summary_field_names, fields))


@contextmanager
def conditional_open(f_name, mode, cond):
    """
    A context manager to conditionally open a file.
    """
    if cond:
        resource = open(f_name, mode)
        try:
            yield resource
        finally:
            resource.close()
    else:
        yield None


class Writer(Thread):

    def __init__(self, iterator, aligner, model, fd=sys.stdout, fastq=False, duplex=False, trim=False):
        super().__init__()
        self.fd = fd
        self.log = []
        self.fastq = fastq
        self.duplex = duplex
        self.aligner = aligner
        self.model = model
        self.iterator = iterator
        self.trim = trim
        self.write_headers()

    def write_headers(self):
        if self.aligner:
            write_sam_header(self.aligner, fd=self.fd)

    def run(self):

        if self.trim:
            trimmed_fast5s_dir = trim_outfiles()[0]
            os.makedirs(trimmed_fast5s_dir, exist_ok=True)

        with CSVLogger(summary_file(), sep='\t') as summary, conditional_open(trim_outfiles()[1], 'a', cond=self.trim) as refs_file:
            for read, res in self.iterator:

                seq = res['sequence']
                qstring = res.get('qstring', '*')
                mean_qscore = res.get('mean_qscore', 0.0)
                mapping = res.get('mapping', False)

                if self.duplex:
                    samples = len(read[0].signal) + len(read[1].signal)
                    read_id = '%s;%s' % (read[0].read_id, read[1].read_id)
                else:
                    samples = len(read.signal)
                    read_id = read.read_id

                if len(seq):
                    if self.aligner:
                        write_sam(read_id, seq, qstring, mapping, fd=self.fd, unaligned=mapping is None)
                        if self.trim:
                            write_ref(refs_file, read_id, mapping, self.aligner)
                            write_trimmed_fast5(trimmed_fast5s_dir, read, mapping, self.model, seq)
                    else:
                        if self.fastq:
                            write_fastq(read_id, seq, qstring, fd=self.fd)
                        else:
                            write_fasta(read_id, seq, fd=self.fd)

                    if self.duplex:
                        summary.append(duplex_summary_row(read[0], read[1], len(seq), mean_qscore, alignment=mapping))
                    else:
                        summary.append(summary_row(read, len(seq), mean_qscore, alignment=mapping))

                    self.log.append((read_id, samples))

                else:
                    logger.warn("> skipping empty sequence %s", read_id)


class CTCWriter(Thread):
    """
    CTC writer process that writes output numpy training data.
    """
    def __init__(self, iterator, aligner, min_coverage, min_accuracy, fd=sys.stdout):
        super().__init__()
        self.fd = fd
        self.log = []
        self.aligner = aligner
        self.iterator = iterator
        self.min_coverage = min_coverage
        self.min_accuracy = min_accuracy
        self.write_headers()

    def write_headers(self):
        if self.aligner:
            write_sam_header(self.aligner, fd=self.fd)

    def run(self):

        chunks = []
        targets = []
        lengths = []

        with CSVLogger(summary_file(), sep='\t') as summary:
            for read, ctc_data in self.iterator:

                seq = ctc_data['sequence']
                qstring = ctc_data['qstring']
                mean_qscore = ctc_data['mean_qscore']
                mapping = ctc_data.get('mapping', False)

                self.log.append((read.read_id, len(read.signal)))

                if len(seq) == 0 or mapping is None:
                    continue

                cov = (mapping.q_en - mapping.q_st) / len(seq)
                acc = mapping.mlen / mapping.blen
                refseq = self.aligner.seq(mapping.ctg, mapping.r_st, mapping.r_en)

                if acc < self.min_accuracy or cov < self.min_coverage or 'N' in refseq:
                    continue

                write_sam(read.read_id, seq, qstring, mapping, fd=self.fd, unaligned=mapping is None)
                summary.append(summary_row(read, len(seq), mean_qscore, alignment=mapping))

                if mapping.strand == -1:
                    refseq = revcomp(refseq)

                target = [int(x) for x in refseq.translate({65: '1', 67: '2', 71: '3', 84: '4'})]
                targets.append(target)
                chunks.append(read.signal)
                lengths.append(len(target))

        if len(chunks) == 0:
            sys.stderr.write("> no suitable ctc data to write\n")
            return

        chunks = np.array(chunks, dtype=np.float16)
        targets_ = np.zeros((chunks.shape[0], max(lengths)), dtype=np.uint8)
        for idx, target in enumerate(targets): targets_[idx, :len(target)] = target
        lengths = np.array(lengths, dtype=np.uint16)
        indices = np.random.permutation(typical_indices(lengths))

        chunks = chunks[indices]
        targets_ = targets_[indices]
        lengths = lengths[indices]

        summary = pd.read_csv(summary_file(), sep='\t')
        summary.iloc[indices].to_csv(summary_file(), sep='\t', index=False)

        output_directory = '.' if sys.stdout.isatty() else dirname(realpath('/dev/fd/1'))
        np.save(os.path.join(output_directory, "chunks.npy"), chunks)
        np.save(os.path.join(output_directory, "references.npy"), targets_)
        np.save(os.path.join(output_directory, "reference_lengths.npy"), lengths)

        sys.stderr.write("> written ctc training data\n")
        sys.stderr.write("  - chunks.npy with shape (%s)\n" % ','.join(map(str, chunks.shape)))
        sys.stderr.write("  - references.npy with shape (%s)\n" % ','.join(map(str, targets_.shape)))
        sys.stderr.write("  - reference_lengths.npy shape (%s)\n" % ','.join(map(str, lengths.shape)))

    def stop(self):
        self.join()
