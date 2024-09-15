import os

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
import wandb
from torch import Tensor
from torch.utils.data import DataLoader, IterableDataset
from tqdm import tqdm
from transformers import LlamaConfig, LlamaForCausalLM

from train_utils import get_grad_norm, get_optim_cls, print_model_stats, quantize_model


class TokenDataset(IterableDataset):
    def __init__(self, dataset_dir: str, batch_size: int, seq_len: int) -> None:
        super().__init__()
        self.shards = sorted(Path(dataset_dir).glob("*.bin"))
        self.batch_size = batch_size
        self.seq_len = seq_len
        self.toks_per_batch = self.batch_size * (self.seq_len + 1)
        print(f"Found {len(self.shards)} shards of data")

    def _iter_shard(self, shard: Tensor, shuffle: bool):
        n_slices = math.floor(shard.shape[0] / self.toks_per_batch)
        slice_indices = torch.randperm(n_slices) if shuffle else range(n_slices)

        for slice_idx in slice_indices:
            batch = shard[slice_idx * self.toks_per_batch : (slice_idx + 1) * self.toks_per_batch]
            batch = batch.view(self.batch_size, self.seq_len + 1)
            tokens = batch[:, :-1].long()
            labels = batch[:, 1:].long()
            yield tokens, labels

    def __iter__(self):
        while True:
            # NOTE: we don't split data across workers. just depend on workers having different
            # random seeds to select different slice of data.
            shard_indices = torch.randperm(len(self.shards))
            for shard_idx in shard_indices:
                # divide a shard into n slices of toks_per_batch
                # to make sure the slices are slightly different everytime, we add a random offset
                # offset in np.memmap is in bytes, so need to times 2
                offset = torch.randint(0, self.toks_per_batch, size=(1,)).item()
                shard_np = np.memmap(self.shards[shard_idx], dtype=np.uint16, mode="r", offset=offset * 2)
                shard = torch.from_numpy(shard_np)

                for data in self._iter_shard(shard, shuffle=True):
                    yield data


class EvalTokenDataset(TokenDataset):
    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        assert worker_info is None or worker_info.num_workers == 1, "Please use num_workers<=1"

        # fast validation loss: calculate loss on consecutive, non-overlapping slices
        # the more correct way is to calculate loss for each token with full seq_len context (rolling window)
        for shard_idx in range(len(self.shards)):
            shard_np = np.memmap(self.shards[shard_idx], dtype=np.uint16, mode="r")
            shard = torch.from_numpy(shard_np)

            for data in self._iter_shard(shard, shuffle=False):
                yield data


class LRSchedule:
    def __init__(
        self, lr: float, n_steps: int, warmup: float = 0.0, decay: float = 0.0, decay_type: str = "linear"
    ) -> None:
        self.lr = lr
        self.t1 = int(n_steps * warmup)
        self.t2 = int(n_steps * (1 - decay))
        self.t3 = n_steps
        self.decay_type = decay_type
        assert self.t1 <= self.t2
        assert decay_type in ("linear", "cosine")

    def get_lr(self, step: int) -> float:
        if step < self.t1:
            return self.lr * step / self.t1
        if step < self.t2:
            return self.lr
        if step < self.t3:
            progress = (step - self.t2) / (self.t3 - self.t2)
            if self.decay_type == "linear":
                return self.lr * (1 - progress)
            elif self.decay_type == "cosine":
                return 0.5 * self.lr * (1 + math.cos(progress * math.pi))
        return 0.0


def get_loss(model: LlamaForCausalLM, tokens: Tensor, labels: Tensor):
    logits = model(tokens).logits.flatten(0, 1)
    return torch.nn.functional.cross_entropy(logits, labels.view(-1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # default config is 470M
    parser.add_argument("--d_model", type=int, default=1024)
    parser.add_argument("--depth", type=int, default=24)
    parser.add_argument("--ffn_size", type=int, default=4096)
    parser.add_argument("--head_dim", type=int, default=64)

    parser.add_argument("--int8_mixed_precision", type=json.loads)
    parser.add_argument("--int8_quantized_training", type=json.loads)
    parser.add_argument("--quantize_lm_head", action="store_true")
    parser.add_argument("--activation_checkpointing", action="store_true")

    parser.add_argument("--dataset_dir", required=True)
    parser.add_argument("--n_workers", type=int, default=1)
    parser.add_argument("--n_steps", type=int, default=1000)
    parser.add_argument("--batch_size", type=int, default=4)
    parser.add_argument("--seq_len", type=int, default=2048)
    parser.add_argument("--gradient_accumulation", type=int, default=1)

    parser.add_argument("--optim", default="torch.optim.AdamW")
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--weight_decay", type=float, default=1e-2)
    parser.add_argument("--optim_kwargs", type=json.loads, default=dict())
    parser.add_argument("--lr_schedule_kwargs", type=json.loads)

    parser.add_argument("--val_dataset_dir")
    parser.add_argument("--val_interval", type=int, default=5000)

    parser.add_argument("--ckpt_interval", type=int, default=1000)
    parser.add_argument("--project", default="llm_pretraining")
    parser.add_argument("--run_name")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    assert args.batch_size % args.gradient_accumulation == 0
    if args.seed is not None:
        torch.manual_seed(args.seed)
    if args.profile:
        args.n_steps = 5
    args.torch_version = torch.__version__

    config = LlamaConfig(
        hidden_size=args.d_model,
        intermediate_size=args.ffn_size,
        num_hidden_layers=args.depth,
        num_attention_heads=args.d_model // args.head_dim,
        max_position_embeddings=args.seq_len,
        use_cache=False,
    )
    model = LlamaForCausalLM(config).bfloat16().cuda()
    if args.activation_checkpointing:
        model.gradient_checkpointing_enable()
    quantize_model(model.model, args.int8_mixed_precision, args.int8_quantized_training)
    if args.quantize_lm_head:
        quantize_model(model.lm_head, args.int8_mixed_precision, args.int8_quantized_training)
    print_model_stats(model)

    optim_cls = get_optim_cls(args.optim)
    optim = optim_cls(model.parameters(), lr=args.lr, weight_decay=args.weight_decay, **args.optim_kwargs)
    lr_schedule = (
        LRSchedule(args.lr, args.n_steps, **args.lr_schedule_kwargs) if args.lr_schedule_kwargs is not None else None
    )

    ds = TokenDataset(args.dataset_dir, args.batch_size, args.seq_len)
    dloader = iter(DataLoader(ds, batch_size=None, num_workers=args.n_workers, pin_memory=True))

    save_dir = Path("runs/llm_pretrain") / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.run_name}"
    save_dir.mkdir(parents=True, exist_ok=True)
    run = wandb.init(
        dir="/tmp",
        config=args,
        project=args.project,
        name=args.run_name,
        mode="disabled" if args.profile else None,
    )

    step = 0
    log_interval = 50
    bsize = args.batch_size // args.gradient_accumulation
    pbar = tqdm(total=args.n_steps, dynamic_ncols=True)
    model.train()
    time0 = time.time()
    if args.profile:
        prof = torch.profiler.profile()

    while step < args.n_steps:
        for _ in range(args.gradient_accumulation):
            tokens, labels = next(dloader)
            loss = torch.compile(get_loss)(model, tokens.cuda(), labels.cuda())
            loss.backward()

        if lr_schedule is not None:
            lr = lr_schedule.get_lr(step)
            for param_group in optim.param_groups:
                if isinstance(param_group["lr"], torch.Tensor):
                    param_group["lr"].fill_(lr)
                else:
                    param_group["lr"] = lr

        if step % log_interval == 0:
            log_dict = dict(
                loss=loss.item(),
                grad_norm=get_grad_norm(model),
                lr=optim.param_groups[0]["lr"],
                num_tokens_seen_millions=args.batch_size * args.seq_len * step / 1e6,
                max_memory_allocated=torch.cuda.max_memory_allocated(),
            )
            if step > 0:
                time1 = time.time()
                log_dict["tokens_per_second"] = args.batch_size * args.seq_len * log_interval / (time1 - time0)
                time0 = time1
            run.log(log_dict, step=step)
            pbar.set_postfix(loss=log_dict["loss"])

        optim.step()
        optim.zero_grad()

        step += 1
        pbar.update()
        if args.profile and step == 1:
            prof.start()

        if args.ckpt_interval > 0 and step % args.ckpt_interval == 0:
            ckpt = dict(
                model=model.state_dict(),
                optim=optim.state_dict(),
                step=step,
            )
            torch.save(ckpt, save_dir / "last.pth")

        if args.val_interval > 0 and step % args.val_interval == 0 and args.val_dataset_dir is not None:
            val_ds = EvalTokenDataset(args.val_dataset_dir, args.batch_size, args.seq_len)
            val_dloader = DataLoader(val_ds, batch_size=None, num_workers=1)

            total_loss = 0
            n_batches = 0
            model.eval()
            with torch.no_grad():
                for tokens, labels in tqdm(val_dloader, desc="Evaluating", dynamic_ncols=True):
                    total_loss += torch.compile(get_loss)(model, tokens.cuda(), labels.cuda()).item()
                    n_batches += 1
            val_loss = total_loss / n_batches
            run.log(dict(val_loss=val_loss), step=step)
            model.train()

    run.finish()
    if args.profile:
        prof.stop()
        prof.export_chrome_trace("trace.json.gz")
