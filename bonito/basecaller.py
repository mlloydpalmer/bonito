"""
Bonito Basecaller
"""

import sys
import time
from math import ceil
from glob import glob
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter

from bonito.decode import DecoderWriter
from bonito.util import load_model, chunk_data, stitch, get_raw_data

import torch
import numpy as np
from tqdm import tqdm


def main(args):

    sys.stderr.write("> loading model\n")
    model = load_model(args.model_directory, args.device, weights=int(args.weights), half=args.half)

    samples = 0
    num_reads = 0
    max_read_size = 1e9
    dtype = np.float16 if args.half else np.float32

    t0 = time.perf_counter()

    sys.stderr.write("> calling\n")

    with DecoderWriter(model.alphabet, args.beamsize) as decoder, torch.no_grad():
        for fast5 in tqdm(glob("%s/*fast5" % args.reads_directory), ascii=True, ncols=100):
            for read_id, raw_data in get_raw_data(fast5):

                if len(raw_data) > max_read_size:
                    sys.stderr.write("> skipping %s: %s too long\n" % (len(raw_data), read_id))
                    pass

                num_reads += 1
                samples += len(raw_data)

                raw_data = raw_data[np.newaxis, np.newaxis, :].astype(dtype)
                gpu_data = torch.tensor(raw_data).to(args.device)
                posteriors = model(gpu_data).exp().cpu().numpy().squeeze()

                decoder.queue.put((read_id, posteriors))

    duration = time.perf_counter() - t0

    sys.stderr.write("> completed reads: %s\n" % num_reads)
    sys.stderr.write("> samples per second %.1E\n" % (samples  / duration))
    sys.stderr.write("> done\n")


def argparser():
    parser = ArgumentParser(
        formatter_class=ArgumentDefaultsHelpFormatter,
        add_help=False
    )
    parser.add_argument("model_directory")
    parser.add_argument("reads_directory")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--weights", default="0", type=str)
    parser.add_argument("--beamsize", default=5, type=int)
    parser.add_argument("--half", action="store_true", default=False)
    return parser
