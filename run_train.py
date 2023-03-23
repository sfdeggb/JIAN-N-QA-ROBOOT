"""
功能：进行模型的训练
author:ZhuJianhao
time:2022-7-8
"""

# coding=utf-8

import os
import logging
import glob
import math
import json
import argparse #to phrase the params form cmd
import random
from pathlib import Path
from tqdm import tqdm, trange#进度条库
import numpy as np
import torch#神经网络的训练
from torch.utils.data import RandomSampler
from torch.utils.data.distributed import DistributedSampler
import torch.distributed as dist#进行分布式计算
import utils_seq2seq
from tokenization_unilm import UnilmTokenizer, WhitespaceTokenizer
from modeling_unilm import UnilmForSeq2Seq, UnilmConfig
#进行现有模型的加载
#AdamW负责模型的优化
#
from transformers import AdamW, get_linear_schedule_with_warmup
try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter



ALL_MODELS = sum((tuple(conf.pretrained_config_archive_map.keys())
                  for conf in (UnilmConfig,)), ())
MODEL_CLASSES = {
    'unilm': (UnilmConfig, UnilmForSeq2Seq, UnilmTokenizer)
}
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


def _get_max_epoch_model(output_dir):
    fn_model_list = glob.glob(os.path.join(output_dir, "model.*.bin"))
    fn_optim_list = glob.glob(os.path.join(output_dir, "optim.*.bin"))
    if (not fn_model_list) or (not fn_optim_list):
        return None
    both_set = set([int(Path(fn).stem.split('.')[-1]) for fn in fn_model_list]
                   ) & set([int(Path(fn).stem.split('.')[-1]) for fn in fn_optim_list])
    if both_set:
        return max(both_set)
    else:
        return None


def main():
    parser = argparse.ArgumentParser()
    tb_writer = SummaryWriter()
    # analyse the params form cmd 
    parser.add_argument("--data_dir", default=None, type=str, required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--src_file", default=None, type=str,
                        help="The input data file name.")
    parser.add_argument("--model_type", default=None, type=str, required=True,
                        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()))
    parser.add_argument("--model_name_or_path", default=None, type=str, required=True,
                        help="Path to pre-trained model or shortcut name selected in the list: " + ", ".join(ALL_MODELS))
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument("--log_dir", default='', type=str,
                        help="The output directory where the log will be written.")
    parser.add_argument("--model_recover_path", default=None, type=str,
                        help="The file of fine-tuned pretraining model.")
    parser.add_argument("--optim_recover_path", default=None, type=str,
                        help="The file of pretraining optimizer.")
    parser.add_argument("--config_name", default="", type=str,
                        help="Pretrained config name or path if not the same as model_name")
    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Pretrained tokenizer name or path if not the same as model_name")

    # Other parameters
    parser.add_argument("--max_seq_length", default=128, type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument('--max_position_embeddings', type=int, default=None,
                        help="max position embeddings")
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case", action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size", default=32, type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size", default=64, type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--label_smoothing", default=0, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.01, type=float,
                        help="The weight decay rate for Adam.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion", default=0.1, type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--hidden_dropout_prob", default=0.1, type=float,
                        help="Dropout rate for hidden states.")
    parser.add_argument("--attention_probs_dropout_prob", default=0.1, type=float,
                        help="Dropout rate for attention probabilities.")
    parser.add_argument("--no_cuda", action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                        "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument('--tokenized_input', action='store_true',
                        help="Whether the input is tokenized.")
    parser.add_argument('--max_len_a', type=int, default=0,
                        help="Truncate_config: maximum length of segment A.")
    parser.add_argument('--max_len_b', type=int, default=0,
                        help="Truncate_config: maximum length of segment B.")
    parser.add_argument('--trunc_seg', default='',
                        help="Truncate_config: first truncate segment A/B (option: a, b).")
    parser.add_argument('--always_truncate_tail', action='store_true',
                        help="Truncate_config: Whether we should always truncate tail.")
    parser.add_argument("--mask_prob", default=0.20, type=float,
                        help="Number of prediction is sometimes less than max_pred when sequence is short.")
    parser.add_argument("--mask_prob_eos", default=0, type=float,
                        help="Number of prediction is sometimes less than max_pred when sequence is short.")
    parser.add_argument('--max_pred', type=int, default=20,
                        help="Max tokens of prediction.")
    parser.add_argument("--num_workers", default=0, type=int,
                        help="Number of workers for the data loader.")
    parser.add_argument('--mask_source_words', action='store_true',
                        help="Whether to mask source words for training")
    parser.add_argument('--skipgram_prb', type=float, default=0.0,
                        help='prob of ngram mask')
    parser.add_argument('--skipgram_size', type=int, default=1,
                        help='the max size of ngram mask')
    parser.add_argument('--mask_whole_word', action='store_true',
                        help="Whether masking a whole word.")
    parser.add_argument('--logging_steps', type=int, default=5,
                        help='the max size of ngram mask')

    args = parser.parse_args()
    #judge the input params
    if not(args.model_recover_path and 
           Path(args.model_recover_path).exists()):
        args.model_recover_path = None

    args.output_dir = args.output_dir.replace(
        '[PT_OUTPUT_DIR]', os.getenv('PT_OUTPUT_DIR', ''))
    
    args.log_dir = args.log_dir.replace(
        '[PT_OUTPUT_DIR]', os.getenv('PT_OUTPUT_DIR', ''))
    os.makedirs(args.output_dir, exist_ok=True)
    
    if args.log_dir:
        os.makedirs(args.log_dir, exist_ok=True)
        
    json.dump(args.__dict__, 
              open(os.path.join(args.output_dir, 'opt.json'), 'w'),
                    sort_keys=True, indent=2)

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device(
            "cuda" if torch.cuda.is_available() and 
            not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        dist.init_process_group(backend='nccl')
        
    logger.info(
        "device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = int(
        args.train_batch_size / args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)
        
    if not args.do_train and not args.do_eval:
        raise ValueError(
            "At least one of `do_train` or `do_eval` must be True.")

    if args.local_rank not in (-1, 0):
        # Make sure only the first process in distributed training will download model & vocab
        dist.barrier()
        
    args.model_type = args.model_type.lower()
    
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(
        args.config_name if args.config_name 
        else args.model_name_or_path, 
        max_position_embeddings=args.max_position_embeddings, 
        label_smoothing=args.label_smoothing)
    
    tokenizer = tokenizer_class.from_pretrained(
        args.tokenizer_name if args.tokenizer_name 
        else args.model_name_or_path, do_lower_case=args.do_lower_case)
    data_tokenizer = (WhitespaceTokenizer() if args.tokenized_input 
                      else tokenizer)
    if args.local_rank == 0:
        dist.barrier()
        
    ## the judgement of params is end!!!
    
    if args.do_train:
        print("Loading Train Dataset", args.data_dir)
        bi_uni_pipeline = [utils_seq2seq.Preprocess4Seq2seq(
            args.max_pred, 
            args.mask_prob, 
            list(tokenizer.vocab.keys()), 
            tokenizer.convert_tokens_to_ids, 
            args.max_seq_length, 
            mask_source_words=False, 
            skipgram_prb=args.skipgram_prb, 
            skipgram_size=args.skipgram_size, 
            mask_whole_word=args.mask_whole_word, 
            tokenizer=data_tokenizer)
            ]

        file = os.path.join(
            args.data_dir, 
            args.src_file if args.src_file else 'train.tgt')
        
        #prepare the dataset 
        train_dataset = utils_seq2seq.Seq2SeqDataset(
            file, args.train_batch_size, 
            data_tokenizer, 
            args.max_seq_length, 
            bi_uni_pipeline=bi_uni_pipeline)
        
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_dataset, replacement=False)
            _batch_size = args.train_batch_size
        else:
            train_sampler = DistributedSampler(train_dataset)
            _batch_size = args.train_batch_size // dist.get_world_size()
        #load the dataset formally
        train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size=_batch_size, sampler=train_sampler,
                                                       num_workers=args.num_workers, collate_fn=utils_seq2seq.batch_list_to_batch_tensors, pin_memory=False)

    # note: args.train_batch_size has been changed to (/= args.gradient_accumulation_steps)
    # t_total = int(math.ceil(len(train_dataset.ex_list) / args.train_batch_size)
    t_total = int(len(train_dataloader) * args.num_train_epochs /
                  args.gradient_accumulation_steps)

    # Prepare model
    recover_step = _get_max_epoch_model(args.output_dir)
    if args.local_rank not in (-1, 0):
        # Make sure only the first process in distributed training will download model & vocab
        dist.barrier()
    global_step = 0
    if (recover_step is None) and (args.model_recover_path is None):
        model_recover = None
    else:
        if recover_step:
            logger.info("***** Recover model: %d *****", recover_step)
            model_recover = torch.load(
                os.path.join(args.output_dir, 
                             "model.{0}.bin".format(recover_step)),
                map_location='cpu')
            
            # recover_step == number of epochs
            global_step = math.floor(
                recover_step * t_total / args.num_train_epochs)
            
        elif args.model_recover_path:
            logger.info("***** Recover model: %s *****",
                        args.model_recover_path)
            model_recover = torch.load(
                args.model_recover_path, map_location='cpu')
            
    #get the model from the dataset
    #use UnilmForSeq2Seq to pretrained the model
    model = model_class.from_pretrained(
        args.model_name_or_path, 
        state_dict=model_recover, 
        config=config)
    if args.local_rank == 0:
        dist.barrier()

    model.to(device)

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(
            nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(
            nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    # Adam优化算法
    optimizer = AdamW(optimizer_grouped_parameters,
                      lr=args.learning_rate, eps=args.adam_epsilon)
    #创建一个学习率从优化器中设置的初始lr线性下降到0的计划
    #预热期，在此期间它从0线性增加到优化器中设置的初始lr
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=int(args.warmup_proportion*t_total), num_training_steps=t_total)
    
    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(
            model, optimizer, opt_level=args.fp16_opt_level)

    if args.local_rank != -1:
        try:
            from torch.nn.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError("DistributedDataParallel")
        model = DDP(model, device_ids=[
                    args.local_rank], output_device=args.local_rank, find_unused_parameters=True)
    elif n_gpu > 1:
        #在模块级实现数据并行
        model = torch.nn.DataParallel(model)

    if recover_step:
        logger.info("***** Recover optimizer: %d *****", recover_step)
        optim_recover = torch.load(os.path.join(
            args.output_dir, "optim.{0}.bin".format(recover_step)), map_location='cpu')
        if hasattr(optim_recover, 'state_dict'):
            optim_recover = optim_recover.state_dict()
        optimizer.load_state_dict(optim_recover)

        logger.info("***** Recover amp: %d *****", recover_step)
        amp_recover = torch.load(os.path.join(
            args.output_dir, "amp.{0}.bin".format(recover_step)), map_location='cpu')
        amp.load_state_dict(amp_recover)

        logger.info("***** Recover scheduler: %d *****", recover_step)
        scheduler_recover = torch.load(os.path.join(
            args.output_dir, "sched.{0}.bin".format(recover_step)), map_location='cpu')
        scheduler.load_state_dict(scheduler_recover)

    logger.info("***** CUDA.empty_cache() *****")
    torch.cuda.empty_cache()

    #traing the model formally
    if args.do_train:
        logger.info("***** Running training *****")
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", t_total)

        model.train()
        if recover_step:
            start_epoch = recover_step+1
        else:
            start_epoch = 1
        tr_loss, logging_loss = 0.0, 0.0
        for i_epoch in trange(start_epoch, 
                              int(args.num_train_epochs)+1,
                              desc="Epoch", disable=args.local_rank not in (-1, 0)):
            if args.local_rank != -1:
                train_sampler.set_epoch(i_epoch)
            #show the deal processing 
            iter_bar = tqdm(train_dataloader, 
                            desc='Iter (loss=X.XXX)',
                            disable=args.local_rank not in (-1, 0))
            for step, batch in enumerate(iter_bar):
                batch = [
                    t.to(device) if t is not None else None for t in batch]
                input_ids, segment_ids, input_mask, lm_label_ids, masked_pos, masked_weights, _ = batch
                #get the loss of model
                masked_lm_loss = model(input_ids, segment_ids, input_mask, lm_label_ids,
                                       masked_pos=masked_pos, masked_weights=masked_weights)
                if n_gpu > 1:
                    masked_lm_loss = masked_lm_loss.mean()
                loss = masked_lm_loss
                tr_loss += loss.item()
                iter_bar.set_description('Iter (loss=%5.3f)' % loss.item())
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                if args.fp16:
                    with amp.scale_loss(loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                    torch.nn.utils.clip_grad_norm_(
                        amp.master_params(optimizer), args.max_grad_norm)
                else:
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(
                        model.parameters(), args.max_grad_norm)

                if (step + 1) % args.gradient_accumulation_steps == 0:
                    optimizer.step()
                    scheduler.step()
                    optimizer.zero_grad()
                    global_step += 1
                    if args.logging_steps > 0 and global_step % args.logging_steps == 0:
                        tb_writer.add_scalar("lr", scheduler.get_lr()[0], global_step)
                        tb_writer.add_scalar("loss", (tr_loss - logging_loss)/args.logging_steps, global_step)
                        logging_loss = tr_loss

            # Save a trained model
            if (args.local_rank == -1 or torch.distributed.get_rank() == 0):
                logger.info("** ** * Saving fine-tuned model and optimizer ** ** * ")
                model_to_save = model.module if hasattr(model, 'module') else model
                output_model_file = os.path.join(args.output_dir, "model.{0}.bin".format(i_epoch))
                #save the model
                torch.save(model_to_save.state_dict(), output_model_file)
                output_optim_file = os.path.join(args.output_dir, "optim.{0}.bin".format(i_epoch))
                torch.save(optimizer.state_dict(), output_optim_file)
                if args.fp16:
                    output_amp_file = os.path.join(args.output_dir, "amp.{0}.bin".format(i_epoch))
                    torch.save(amp.state_dict(), output_amp_file)
                output_sched_file = os.path.join(args.output_dir, "sched.{0}.bin".format(i_epoch))
                torch.save(scheduler.state_dict(), output_sched_file)
                logger.info("***** CUDA.empty_cache() *****")
                torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
