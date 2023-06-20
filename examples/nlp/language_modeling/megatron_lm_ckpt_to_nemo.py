# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

r"""
Conversion script to convert Megatron_LM checkpoints into nemo checkpoint.
  Example to run this conversion script:
    python -m torch.distributed.launch --nproc_per_node=<tensor_model_parallel_size> megatron_lm_ckpt_to_nemo.py \
     --checkpoint_folder <path_to_PTL_checkpoints_folder> \
     --checkpoint_name megatron_gpt--val_loss=99.99-step={steps}-consumed_samples={consumed}.0 \
     --nemo_file_path <path_to_output_nemo_file> \
     --model_type <megatron model type> \
     --hparams_file <hparams yaml file>
     --tensor_model_parallel_size <tensor_model_parallel_size>
     --pipeline_model_parallel_size <pipeline_model_parallel_size>
     --gpus_per_node  <gpus per node>
Note, hparams_file usually is generated by pytorch lightning when running the training job.
It is the model section of the model pretraining conf with an extra cfg key.
Check https://github.com/NVIDIA/NeMo/issues/4993 for an example.
To resume the training from converted MegatronLM checkpoint, make sure to set the
`trainer.max_steps=round(lr-warmup-fraction * lr-decay-iters + lr-decay-iters)`
where  `lr-warmup-fraction` and `lr-decay-iters` are arguments from MegatronLM training
so the learning rate scheduler will follow the same curve.
"""

import importlib
import os
import pathlib
import sys
from argparse import ArgumentParser
from collections import OrderedDict
from typing import Any, Optional

import torch
from megatron.core import parallel_state
from pytorch_lightning.core.saving import _load_state as ptl_load_state
from pytorch_lightning.core.saving import load_hparams_from_tags_csv, load_hparams_from_yaml
from pytorch_lightning.trainer.trainer import Trainer
from pytorch_lightning.utilities.cloud_io import load as pl_load
from pytorch_lightning.utilities.migration import pl_legacy_patch

from nemo.collections.nlp.models.language_modeling.megatron_bert_model import MegatronBertModel
from nemo.collections.nlp.models.language_modeling.megatron_gpt_model import MegatronGPTModel
from nemo.collections.nlp.modules.common.megatron.megatron_init import initialize_model_parallel_for_nemo
from nemo.collections.nlp.parts.nlp_overrides import NLPSaveRestoreConnector
from nemo.utils import AppState, logging
from nemo.utils.distributed import initialize_distributed
from nemo.utils.model_utils import inject_model_parallel_rank, uninject_model_parallel_rank

# this enums code is copied from Megatron_LM
enum_code = '''
import enum

class ModelType(enum.Enum):
    encoder_or_decoder = 1
    encoder_and_decoder = 2


class LayerType(enum.Enum):
    encoder = 1
    decoder = 2


class AttnType(enum.Enum):
    self_attn = 1
    cross_attn = 2


class AttnMaskType(enum.Enum):
    padding = 1
    causal = 2
'''


def install_megatron_dependence():
    # this is a hack to install required modules for MegatronLM checkpoints
    # run the following so we don't have to install Megatron_LM code
    megatron_name = 'megatron'
    megatron_spec = importlib.util.spec_from_loader(megatron_name, loader=None, is_package=True)

    megatron_module = importlib.util.module_from_spec(megatron_spec)
    sys.modules[megatron_name] = megatron_module

    model_name = 'model'
    model_spec = importlib.util.spec_from_loader(model_name, loader=None, is_package=True)

    model_module = importlib.util.module_from_spec(model_spec)

    megatron_module.__dict__['model'] = model_module

    sys.modules[megatron_name + '.' + model_name] = model_module

    enums_name = 'enums'
    enums_spec = importlib.util.spec_from_loader(enums_name, loader=None, is_package=True)
    enums_module = importlib.util.module_from_spec(enums_spec)

    model_module.__dict__['enums'] = enums_module

    sys.modules[megatron_name + '.' + model_name + '.' + enums_name] = enums_module

    exec(enum_code, enums_module.__dict__)


def get_args():
    parser = ArgumentParser()
    parser.add_argument(
        "--checkpoint_folder",
        type=str,
        default=None,
        required=True,
        help="Path to Megatron-LM checkpoints saved during training. Ex: /raid/Megatron_LM/checkpoints",
    )
    parser.add_argument(
        "--checkpoint_name",
        type=str,
        default='model_optim_rng.pt',
        required=True,
        help="Name of checkpoint to be used. Ex: model_optim_rng.pt",
    )

    parser.add_argument(
        "--hparams_file",
        type=str,
        default=None,
        required=False,
        help="Path config for restoring. It's created during training and may need to be modified during restore if restore environment is different than training. Ex: /raid/nemo_experiments/megatron_gpt/hparams.yaml",
    )
    parser.add_argument("--nemo_file_path", type=str, default=None, required=False, help="Path to output .nemo file.")

    parser.add_argument(
        "--output_ckpt_file_path", type=str, default=None, required=False, help="Path to output .ckpt file."
    )

    parser.add_argument("--gpus_per_node", type=int, required=False, default=1)

    parser.add_argument("--tensor_model_parallel_size", type=int, required=True, default=None)
    parser.add_argument("--pipeline_model_parallel_size", type=int, required=False, default=1)

    parser.add_argument("--local_rank", type=int, required=False, default=os.getenv('LOCAL_RANK', -1))

    parser.add_argument("--model_type", type=str, required=True, default="gpt", choices=["gpt", "t5", "bert"])

    args = parser.parse_args()
    return args


def parse_weights(weight_dict: OrderedDict, parent_key: str, total: list, converted: OrderedDict, translator: dict):
    for key in weight_dict:
        new_key = key
        name_translate = translator

        for replace_key in name_translate:
            if key.find(replace_key) >= 0:
                new_key = key.replace(replace_key, name_translate[replace_key])
        if isinstance(weight_dict[key], OrderedDict) or isinstance(weight_dict[key], dict):
            parse_weights(weight_dict[key], parent_key + '.' + new_key, total, converted, translator)
        else:
            num_parameters = torch.prod(torch.tensor(weight_dict[key].cpu().size())).item()
            total[0] += num_parameters
            final_key = 'model' + parent_key + '.' + new_key
            converted[final_key] = weight_dict[key]


def add_optimizer_state(lm_checkpoint, new_checkpoint, megatron_amp_o2=True):
    # this method is to convert lm_checkpoint optimizer states for nemo checkpoint
    OPTIMIZER_KEY = 'optimizer'
    FP32_FP16_KEY = 'fp32_from_fp16_params'
    NEW_OPTIMIZER_KEY = 'optimizer_states'
    STEP_KEY = 'iteration'
    NEW_STEP_KEY = 'global_step'
    LR_SCHEDULER = 'lr_scheduler'
    NEW_LR_SCHEDULER = 'lr_schedulers'
    if OPTIMIZER_KEY in lm_checkpoint and OPTIMIZER_KEY in lm_checkpoint[OPTIMIZER_KEY]:
        opt_state = lm_checkpoint[OPTIMIZER_KEY][OPTIMIZER_KEY]
        if megatron_amp_o2:
            opt_dict = dict()
            if LR_SCHEDULER in lm_checkpoint:
                sched = lm_checkpoint[LR_SCHEDULER]
                for param_group in opt_state['param_groups']:
                    param_group['initial_lr'] = sched['max_lr']
            if FP32_FP16_KEY in lm_checkpoint[OPTIMIZER_KEY]:
                fp32_state = lm_checkpoint[OPTIMIZER_KEY][FP32_FP16_KEY]
                opt_dict[FP32_FP16_KEY] = fp32_state
            opt_dict[OPTIMIZER_KEY] = opt_state
            new_checkpoint[NEW_OPTIMIZER_KEY] = [opt_dict]
        else:
            new_checkpoint[NEW_OPTIMIZER_KEY] = [opt_state]

    if STEP_KEY in lm_checkpoint:
        new_checkpoint[NEW_STEP_KEY] = lm_checkpoint[STEP_KEY]
        new_checkpoint['epoch'] = 1  # always one epoch
    if LR_SCHEDULER in lm_checkpoint:
        gbs = lm_checkpoint['args'].global_batch_size
        sched = lm_checkpoint[LR_SCHEDULER]
        content = OrderedDict()
        content['max_steps'] = int(sched['decay_steps']) // gbs + sched['warmup_steps'] // gbs
        content['warmup_steps'] = int(sched['warmup_steps']) // gbs
        content['constant_steps'] = 0  # no such conf in lm checkpoint
        content['decay_steps'] = int(sched['decay_steps']) // gbs
        content['min_lr'] = sched['min_lr']
        if OPTIMIZER_KEY in lm_checkpoint:
            content['base_lrs'] = [
                i['initial_lr'] for i in new_checkpoint['optimizer_states'][0]['optimizer']['param_groups']
            ]
            content['last_epoch'] = int(sched['num_steps']) // gbs
            content['_last_lr'] = [i['lr'] for i in new_checkpoint['optimizer_states'][0]['optimizer']['param_groups']]
        else:
            content['base_lrs'] = [sched['max_lr']]
            content['last_epoch'] = int(sched['num_steps']) // gbs
        content['_step_count'] = int(sched['num_steps']) // gbs
        content['verbose'] = False
        content['_get_lr_called_within_step'] = False
        new_checkpoint[NEW_LR_SCHEDULER] = [content]


def load_model(cls, checkpoint, strict, **kwargs):
    try:
        if 'cfg' in kwargs:
            model = ptl_load_state(cls, checkpoint, strict=strict, **kwargs)
        else:
            model = ptl_load_state(
                cls, checkpoint, strict=strict, cfg=checkpoint[cls.CHECKPOINT_HYPER_PARAMS_KEY].cfg, **kwargs
            )
            # register the artifacts
            cfg = checkpoint[cls.CHECKPOINT_HYPER_PARAMS_KEY].cfg
            if cfg.tokenizer.model is not None:
                model.register_artifact("tokenizer.tokenizer_model", cfg.tokenizer.model)
            if cfg.tokenizer.vocab_file is not None:
                model.register_artifact("tokenizer.vocab_file", cfg.tokenizer.vocab_file)
            if cfg.tokenizer.merge_file is not None:
                model.register_artifact("tokenizer.merge_file", cfg.tokenizer.merge_file)
    finally:
        cls._set_model_restore_state(is_being_restored=False)
    return model


def load_from_checkpoint(
    cls,
    checkpoint_path: str,
    map_location: Any = None,
    hparams_file: Optional[str] = None,
    strict: bool = True,
    **kwargs,
):
    """
        Loads Megatron_LM checkpoints, convert it, with some maintenance of restoration.
        For documentation, please refer to LightningModule.load_from_checkpoin() documentation.
        """
    checkpoint = None
    try:
        cls._set_model_restore_state(is_being_restored=True)
        # TODO: replace with proper PTL API

        with pl_legacy_patch():
            if map_location is not None:
                old_checkpoint = pl_load(checkpoint_path, map_location=map_location)
            else:
                old_checkpoint = pl_load(checkpoint_path, map_location=lambda storage, loc: storage)

        total_params = [0]
        checkpoint = OrderedDict()
        checkpoint['state_dict'] = OrderedDict()
        parse_weights(
            old_checkpoint['model'], "", total_params, checkpoint['state_dict'], translator=kwargs['translator']
        )
        print('converted {:.2f}M parameters'.format(total_params[0] / 1e6))

        if hparams_file is not None:
            extension = hparams_file.split(".")[-1]
            if extension.lower() == "csv":
                hparams = load_hparams_from_tags_csv(hparams_file)
            elif extension.lower() in ("yml", "yaml"):
                hparams = load_hparams_from_yaml(hparams_file)
            else:
                raise ValueError(".csv, .yml or .yaml is required for `hparams_file`")

            hparams["on_gpu"] = False

            # overwrite hparams by the given file
            checkpoint[cls.CHECKPOINT_HYPER_PARAMS_KEY] = hparams

        check_point_version = old_checkpoint.get('checkpoint_version', 0)
        if check_point_version < 3:
            # need to do the transpose of query_key_value variables
            if hparams_file is not None:
                np = hparams['cfg']['num_attention_heads']
            elif 'config' in old_checkpoint and 'num-attention-heads' in old_checkpoint['config']:
                np = old_checkpoint['config']['num-attention-heads']

            else:
                logging.warning("cannot determine the number attention heads")
                raise ValueError('need to know number of attention heads')

            if check_point_version == 0:
                # 3, np, hn -> np, 3, hn
                for key in checkpoint['state_dict']:
                    if key.find('query_key_value') >= 0:
                        weight = checkpoint['state_dict'][key]
                        if len(weight.size()) == 2:
                            # weight
                            weight = weight.view(3, np, -1, weight.size()[-1])
                            weight = weight.transpose(0, 1).contiguous()
                            checkpoint['state_dict'][key] = weight.view(-1, weight.size()[-1])
                        else:
                            # biase
                            weight = weight.view(3, np, -1)
                            weight = weight.transpose(0, 1).contiguous()
                            checkpoint['state_dict'][key] = weight.view(-1)
            elif check_point_version == 1:
                # np, hn, 3 -> np, 3, hn
                for key in checkpoint['state_dict']:
                    if key.find('query_key_value') >= 0:
                        weight = checkpoint['state_dict'][key]
                        if len(weight.size()) == 2:
                            # weight
                            weight = weight.view(np, -1, 3, weight.size()[-1])
                            weight = weight.transpose(1, 2).contiguous()
                            checkpoint['state_dict'][key] = weight
                        else:
                            # biase
                            weight = weight.view(np, -1, 3)
                            weight = weight.transpose(1, 2).contiguous()
                            checkpoint['state_dict'][key] = weight

        # for past checkpoint need to add the new key
        if cls.CHECKPOINT_HYPER_PARAMS_KEY not in checkpoint:
            checkpoint[cls.CHECKPOINT_HYPER_PARAMS_KEY] = {}
        # override the hparams with values that were passed in
        # TODO: can we do this without overriding?
        config_kwargs = kwargs.copy()
        if 'trainer' in config_kwargs:
            config_kwargs.pop('trainer')
        checkpoint[cls.CHECKPOINT_HYPER_PARAMS_KEY].update(config_kwargs)
        add_optimizer_state(old_checkpoint, checkpoint)
        consumed = None
        if 'args' in old_checkpoint and hasattr(old_checkpoint['args'], 'consumed_train_samples'):
            consumed = getattr(old_checkpoint['args'], 'consumed_train_samples')
        steps = None
        if 'iteration' in old_checkpoint:
            steps = old_checkpoint['iteration']
    finally:
        cls._set_model_restore_state(is_being_restored=False)
    logging.warning(f"the checkpoint version is {check_point_version}")
    return checkpoint, consumed, steps, check_point_version


def megatron_lm_inject_model_parallel_rank(filepath):
    """
    Injects tensor/pipeline model parallel ranks into the filepath.
    Does nothing if not using model parallelism.
    """
    # first make sure filepath does not have rank
    filepath = uninject_model_parallel_rank(filepath)

    app_state = AppState()
    if app_state.model_parallel_size is not None and app_state.model_parallel_size > 1:
        # filepath needs to be updated to include mp_rank
        dirname = os.path.dirname(filepath)
        basename = os.path.basename(filepath)
        if app_state.pipeline_model_parallel_size is None or app_state.pipeline_model_parallel_size == 1:
            filepath = f'{dirname}/mp_rank_{app_state.tensor_model_parallel_rank:02d}/{basename}'
        else:
            filepath = f'{dirname}/mp_rank_{app_state.tensor_model_parallel_rank:02d}_{app_state.pipeline_model_parallel_rank:03d}/{basename}'
        return filepath
    else:
        return filepath


def convert(local_rank, rank, world_size, args):

    app_state = AppState()
    initialize_model_parallel_for_nemo(
        world_size=world_size,
        global_rank=rank,
        local_rank=local_rank,
        tensor_model_parallel_size=args.tensor_model_parallel_size,
        pipeline_model_parallel_size=args.pipeline_model_parallel_size,
        virtual_pipeline_model_parallel_size=None,
        pipeline_model_parallel_split_rank=0,
        micro_batch_size=None,
        global_batch_size=None,
        seed=1234,
        apex_transformer_log_level=30,
    )
    # hard set the data parallel rank to 0, otherwiaze it is default to None
    app_state.data_parallel_rank = 0

    # tensor_model_parallel_size = args.tensor_model_parallel_size
    num_nodes = world_size // args.gpus_per_node
    assert world_size % args.gpus_per_node == 0, "world_size must be divisible by gpus_per_node"

    trainer = Trainer(devices=args.gpus_per_node, accelerator='gpu', num_nodes=num_nodes)
    checkpoint_path = megatron_lm_inject_model_parallel_rank(
        os.path.join(args.checkpoint_folder, args.checkpoint_name)
    )
    logging.info(f"loading checkpoint {checkpoint_path}")

    if args.model_type == 'gpt':
        # this dictionary is used to rename the model parameters
        name_translate = {}
        name_translate['transformer'] = 'encoder'
        name_translate['.attention.'] = '.self_attention.'
        # nemo megatron doesn't have _for_head key
        name_translate['word_embeddings_for_head'] = 'word_embeddings'
        checkpoint, consumed, steps, version = load_from_checkpoint(
            MegatronGPTModel,
            checkpoint_path,
            hparams_file=args.hparams_file,
            trainer=trainer,
            translator=name_translate,
            strict=False,
        )
    elif args.model_type == 'bert':
        # this dictionary is used to rename the model parameters
        name_translate = {}
        name_translate['transformer'] = 'encoder'
        name_translate['.attention.'] = '.self_attention.'
        # nemo megatron doesn't have _for_head key
        name_translate['word_embeddings_for_head'] = 'word_embeddings'
        checkpoint, consumed, steps, version = load_from_checkpoint(
            MegatronBertModel,
            checkpoint_path,
            hparams_file=args.hparams_file,
            trainer=trainer,
            translator=name_translate,
            strict=False,
        )
    else:
        raise NotImplemented("{} is not supported".format(args.model_type))

    if torch.distributed.is_initialized():
        torch.distributed.barrier()

    if args.output_ckpt_file_path:
        filepath = args.output_ckpt_file_path
        base_dir = pathlib.Path(filepath).parent
        filename_str = pathlib.Path(filepath).name
        suffix = '.ckpt'
        content = {}
        if consumed is not None:
            content['consumed'] = consumed
        else:
            content['consumed'] = 0
        if steps is not None:
            content['steps'] = steps
        else:
            content['steps'] = 0
        filename = filename_str.format(**content) + suffix
        checkpoint_path_output = inject_model_parallel_rank(os.path.join(base_dir, filename))
        trainer.training_type_plugin.checkpoint_io.save_checkpoint(checkpoint, checkpoint_path_output)
        logging.info(f'NeMo model checkpoint files saved to: {args.output_ckpt_file_path}')

    if args.nemo_file_path:
        if args.model_type == 'gpt':
            model = load_model(MegatronGPTModel, checkpoint, strict=False, trainer=trainer)
        elif args.model_type == 'bert':
            model = load_model(MegatronBertModel, checkpoint, strict=False, trainer=trainer)
        else:
            raise NotImplemented("{} is not supported".format(args.model_type))

        # verify tensor parallel rank id and pipeline parallel rank id matches
        assert app_state.data_parallel_size == 1
        model._save_restore_connector = NLPSaveRestoreConnector()
        model.save_to(args.nemo_file_path)
        logging.info(f'NeMo model saved to: {args.nemo_file_path}')


if __name__ == '__main__':
    install_megatron_dependence()
    args = get_args()
    if args.local_rank == -1:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        rank = args.local_rank
        local_rank = rank
        world_size = 1
    else:
        local_rank, rank, world_size = initialize_distributed(args)

    # make sure the world size is divisible by tensor model parallel_size
    assert world_size % args.tensor_model_parallel_size == 0

    torch.distributed.barrier()
    convert(local_rank, rank, world_size, args)
    torch.distributed.barrier()
