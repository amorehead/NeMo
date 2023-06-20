# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This script implemts Noisy Channel Reranking (NCR) - https://arxiv.org/abs/1908.05731
Given .nemo files for a, reverse model (target -> source) and transformer LM (target LM) NMT model's .nemo file,
this script can be used to re-rank a forward model's (source -> target) beam candidates.

This script can be used in two ways 1) Given the score file generated by `nmt_transformer_infer.py`, re-rank beam candidates and
2) Given NCR score file generated by 1), Re-rank beam candidates based only on cached scores in the ncr file. This is meant to tune NCR coeficients.

Pre-requisite: Generating translations using `nmt_transformer_infer.py`
1. Obtain text file in src language. You can use sacrebleu to obtain standard test sets like so:
    sacrebleu -t wmt14 -l de-en --echo src > wmt14-de-en.src
2. Translate using `nmt_transformer_infer.py` with a large beam size.:
    python nmt_transformer_infer.py --model=[Path to .nemo file(s)] --srctext=wmt14-de-en.src --tgtout=wmt14-de-en.translations --beam_size 15 --write_scores

USAGE Example (case 1):
Re-rank beam candidates:
    python noisy_channel_reranking.py \
        --reverse_model=[Path to .nemo file] \
        --language_model=[Path to .nemo file] \
        --srctext=wmt14-de-en.translations.scores \
        --tgtout=wmt14-de-en.ncr.translations \
        --forward_model_coef=1.0 \
        --reverse_model_coef=0.7 \
        --target_lm_coef=0.05 \
        --write_scores \

USAGE Example (case 2):
Re-rank beam candidates using cached score file only
    python noisy_channel_reranking.py \
        --cached_score_file=wmt14-de-en.ncr.translations.scores \
        --forward_model_coef=1.0 \
        --reverse_model_coef=0.7 \
        --target_lm_coef=0.05 \
        --tgtout=wmt14-de-en.ncr.translations \
"""


from argparse import ArgumentParser

import numpy as np
import torch

import nemo.collections.nlp as nemo_nlp
from nemo.utils import logging


def score_fusion(args, forward_scores, rev_scores, lm_scores, src_lens, tgt_lens):
    """
    Fuse forward, reverse and language model scores.
    """
    fused_scores = []
    for forward_score, rev_score, lm_score, src_len, tgt_len in zip(
        forward_scores, rev_scores, lm_scores, src_lens, tgt_lens
    ):
        score = 0

        forward_score = forward_score / tgt_len if args.length_normalize_scores else forward_score
        score += args.forward_model_coef * forward_score

        rev_score = rev_score / src_len if args.length_normalize_scores else rev_score
        score += args.reverse_model_coef * rev_score

        lm_score = lm_score / tgt_len if args.length_normalize_scores else lm_score
        score += args.target_lm_coef * lm_score

        if args.len_pen is not None:
            score = score / (((5 + tgt_len) / 6) ** args.len_pen)

        fused_scores.append(score)

    return fused_scores


def main():
    parser = ArgumentParser()
    parser.add_argument(
        "--reverse_model",
        type=str,
        help="Path to .nemo model file(s). If ensembling, provide comma separated paths to multiple models.",
    )
    parser.add_argument(
        "--language_model", type=str, help="Optional path to an LM model that has the same tokenizer as NMT models.",
    )
    parser.add_argument(
        "--forward_model_coef",
        type=float,
        default=1.0,
        help="Weight assigned to the forward NMT model for re-ranking.",
    )
    parser.add_argument(
        "--reverse_model_coef",
        type=float,
        default=0.7,
        help="Weight assigned to the reverse NMT model for re-ranking.",
    )
    parser.add_argument(
        "--target_lm_coef", type=float, default=0.07, help="Weight assigned to the target LM model for re-ranking.",
    )
    parser.add_argument(
        "--srctext",
        type=str,
        default=None,
        help="Path to a TSV file containing forward model scores of the format source \t beam_candidate_i \t forward_score",
    )
    parser.add_argument(
        "--cached_score_file",
        type=str,
        default=None,
        help="Path to a TSV file containing cached scores for each beam candidate. Format source \t target \t forward_score \t reverse_score \t lm_score \t src_len \t tgt_len",
    )
    parser.add_argument(
        "--tgtout", type=str, required=True, help="Path to the file where re-ranked translations are to be written."
    )
    parser.add_argument(
        "--beam_size",
        type=int,
        default=4,
        help="Beam size with which forward model translations were generated. IMPORTANT: mismatch can lead to wrong results and an incorrect number of generated translations.",
    )
    parser.add_argument(
        "--target_lang", type=str, default=None, help="Target language identifier ex: en,de,fr,es etc."
    )
    parser.add_argument(
        "--source_lang", type=str, default=None, help="Source language identifier ex: en,de,fr,es etc."
    )
    parser.add_argument(
        "--write_scores", action="store_true", help="Whether to write forward, reverse and lm scores to a file."
    )
    parser.add_argument(
        "--length_normalize_scores",
        action="store_true",
        help="If true, it will divide forward, reverse and lm scores by the corresponding sequence length.",
    )
    parser.add_argument(
        "--len_pen",
        type=float,
        default=None,
        help="Apply a length penalty based on target lengths to the final NCR score.",
    )

    args = parser.parse_args()
    torch.set_grad_enabled(False)

    if args.cached_score_file is None:
        reverse_models = []
        for model_path in args.reverse_model.split(','):
            if not model_path.endswith('.nemo'):
                raise NotImplementedError(f"Only support .nemo files, but got: {model_path}")
            model = nemo_nlp.models.machine_translation.MTEncDecModel.restore_from(restore_path=model_path).eval()
            model.eval_loss_fn.reduction = 'none'
            reverse_models.append(model)

        lm_model = nemo_nlp.models.language_modeling.TransformerLMModel.restore_from(
            restore_path=args.language_model
        ).eval()

    if args.srctext is not None and args.cached_score_file is not None:
        raise ValueError("Only one of --srctext or --cached_score_file must be provided.")

    if args.srctext is None and args.cached_score_file is None:
        raise ValueError("Neither --srctext nor --cached_score_file were provided.")

    if args.srctext is not None:
        logging.info(f"Re-ranking: {args.srctext}")
    else:
        logging.info(f"Re-ranking from cached score file only: {args.cached_score_file}")

    if args.cached_score_file is None:
        if torch.cuda.is_available():
            reverse_models = [model.cuda() for model in reverse_models]
            lm_model = lm_model.cuda()

    src_text = []
    tgt_text = []
    all_reverse_scores = []
    all_lm_scores = []
    all_forward_scores = []
    all_src_lens = []
    all_tgt_lens = []

    # Chceck args if re-ranking from cached score file.
    if args.cached_score_file is not None:
        if args.write_scores:
            raise ValueError("--write_scores cannot be provided with a cached score file.")
        if args.reverse_model is not None:
            raise ValueError(
                "--reverse_model cannot be provided with a cached score file since it assumes reverse scores already present in the cached file."
            )
        if args.language_model is not None:
            raise ValueError(
                "--language_model cannot be provided with a cached score file since it assumes language model scores already present in the cached file."
            )

    if args.srctext is not None:
        # Compute reverse scores and LM scores from the provided models since cached scores file is not provided.
        with open(args.srctext, 'r') as src_f:
            count = 0
            for line in src_f:
                src_text.append(line.strip().split('\t'))
                if len(src_text) == args.beam_size:
                    # Source and target sequences are flipped for the reverse direction model.
                    src_texts = [item[1] for item in src_text]
                    tgt_texts = [item[0] for item in src_text]
                    src, src_mask = reverse_models[0].prepare_inference_batch(src_texts)
                    tgt, tgt_mask = reverse_models[0].prepare_inference_batch(tgt_texts, target=True)
                    src_lens = src_mask.sum(1).data.cpu().tolist()
                    tgt_lens = tgt_mask.sum(1).data.cpu().tolist()
                    forward_scores = [float(item[2]) for item in src_text]

                    # Ensemble of reverse model scores.
                    nmt_lls = []
                    for model in reverse_models:
                        nmt_log_probs = model(src, src_mask, tgt[:, :-1], tgt_mask[:, :-1])
                        nmt_nll = model.eval_loss_fn(log_probs=nmt_log_probs, labels=tgt[:, 1:])
                        nmt_ll = nmt_nll.view(nmt_log_probs.size(0), nmt_log_probs.size(1)).sum(1) * -1.0
                        nmt_ll = nmt_ll.data.cpu().numpy().tolist()
                        nmt_lls.append(nmt_ll)
                    reverse_scores = np.stack(nmt_lls).mean(0)

                    # LM scores.
                    if lm_model is not None:
                        # Compute LM score for the src of the reverse model.
                        lm_log_probs = lm_model(src[:, :-1], src_mask[:, :-1])
                        lm_nll = model.eval_loss_fn(log_probs=lm_log_probs, labels=src[:, 1:])
                        lm_ll = lm_nll.view(lm_log_probs.size(0), lm_log_probs.size(1)).sum(1) * -1.0
                        lm_ll = lm_ll.data.cpu().numpy().tolist()
                    else:
                        lm_ll = None
                    lm_scores = lm_ll

                    all_reverse_scores.extend(reverse_scores)
                    all_lm_scores.extend(lm_scores)
                    all_forward_scores.extend(forward_scores)

                    # Swapping source and target here back again since this is what gets written to the file.
                    all_src_lens.extend(tgt_lens)
                    all_tgt_lens.extend(src_lens)

                    fused_scores = score_fusion(args, forward_scores, reverse_scores, lm_scores, src_lens, tgt_lens)
                    tgt_text.append(src_texts[np.argmax(fused_scores)])
                    src_text = []
                    count += 1
                    print(f'Reranked {count} sentences')

    else:
        # Use reverse and LM scores from the cached scores file to re-rank.
        with open(args.cached_score_file, 'r') as src_f:
            count = 0
            for line in src_f:
                src_text.append(line.strip().split('\t'))
                if len(src_text) == args.beam_size:
                    if not all([len(item) == 7 for item in src_text]):
                        raise IndexError(
                            "All lines did not contain exactly 5 fields. Format - src_txt \t tgt_text \t forward_score \t reverse_score \t lm_score \t src_len \t tgt_len"
                        )
                    src_texts = [item[0] for item in src_text]
                    tgt_texts = [item[1] for item in src_text]
                    forward_scores = [float(item[2]) for item in src_text]
                    reverse_scores = [float(item[3]) for item in src_text]
                    lm_scores = [float(item[4]) for item in src_text]
                    src_lens = [float(item[5]) for item in src_text]
                    tgt_lens = [float(item[6]) for item in src_text]

                    fused_scores = score_fusion(args, forward_scores, reverse_scores, lm_scores, src_lens, tgt_lens)
                    tgt_text.append(tgt_texts[np.argmax(fused_scores)])
                    src_text = []
                    count += 1
                    print(f'Reranked {count} sentences')

    with open(args.tgtout, 'w') as tgt_f:
        for line in tgt_text:
            tgt_f.write(line + "\n")

    # Write scores file
    if args.write_scores:
        with open(args.tgtout + '.scores', 'w') as tgt_f, open(args.srctext, 'r') as src_f:
            src_lines = []
            for line in src_f:
                src_lines.append(line.strip().split('\t'))
            if not (len(all_reverse_scores) == len(all_lm_scores) == len(all_forward_scores) == len(src_lines)):
                raise ValueError(
                    f"Length of scores files do not match. {len(all_reverse_scores)} != {len(all_lm_scores)} != {len(all_forward_scores)} != {len(src_lines)}. This is most likely because --beam_size is set incorrectly. This needs to be set to the same value that was used to generate translations."
                )
            for f, r, lm, sl, tl, src in zip(
                all_forward_scores, all_reverse_scores, all_lm_scores, all_src_lens, all_tgt_lens, src_lines
            ):
                tgt_f.write(
                    src[0]
                    + '\t'
                    + src[1]
                    + '\t'
                    + str(f)
                    + '\t'
                    + str(r)
                    + '\t'
                    + str(lm)
                    + '\t'
                    + str(sl)
                    + '\t'
                    + str(tl)
                    + '\n'
                )


if __name__ == '__main__':
    main()  # noqa pylint: disable=no-value-for-parameter
