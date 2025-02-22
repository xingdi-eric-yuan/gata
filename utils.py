import torch
import torch.nn as nn
import torch.nn.functional as F
import gym
import textworld.gym

from typing import Optional, List, Sequence, Iterator, TypeVar
from collections import Counter
from textworld import EnvInfos

from preprocessor import SpacyPreprocessor


def load_fasttext(fname: str, preprocessor: SpacyPreprocessor) -> nn.Embedding:
    with open(fname, "r") as f:
        _, emb_dim = map(int, f.readline().split())

        data = {}
        for line in f:
            parts = line.rstrip().split(" ", 1)
            data[parts[0]] = parts[1]
    # embedding for pad is initalized to 0
    # embeddings for OOVs are randomly initialized from N(0, 1)
    emb = nn.Embedding(
        len(preprocessor.word_to_id_dict), emb_dim, padding_idx=preprocessor.pad_id
    )
    for word, i in preprocessor.word_to_id_dict.items():
        if word in data:
            emb.weight[i] = torch.tensor(list(map(float, data[word].split())))
    emb.weight.detach_()
    return emb


def masked_mean(input: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """
    input: (batch, seq_len, hidden_dim)
    mask: (batch, seq_len)
    output: (batch, hidden_dim)
    """
    return (input * mask.unsqueeze(-1)).sum(dim=1) / mask.sum(dim=1, keepdim=True)


def masked_softmax(
    input: torch.Tensor, mask: torch.Tensor, dim: Optional[int] = None
) -> torch.Tensor:
    """
    input, mask and output all have the same dimensions
    """
    # replace the values to be ignored with negative infinity
    return F.softmax(input.masked_fill(mask == 0, float("-inf")), dim=dim)


def generate_square_subsequent_mask(size: int) -> torch.Tensor:
    """
    Generate a square subsequent mask of the given size.
    Useful for attn_mask in MultiheadAttention.

    For example, if size == 3:
    [[False,  True,  True],
     [False, False,  True],
     [False, False, False]]
    """
    return torch.triu(torch.ones(size, size) == 1, diagonal=1)


def calculate_seq_f1(
    pred_obs_word_ids: List[int], ground_truth_obs_word_ids: List[int]
) -> float:
    """
    Taken from the original GATA code and modified.
    """
    if pred_obs_word_ids == ground_truth_obs_word_ids:
        return 1.0
    common = Counter(pred_obs_word_ids) & Counter(ground_truth_obs_word_ids)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = 1.0 * num_same / len(pred_obs_word_ids)
    recall = 1.0 * num_same / len(ground_truth_obs_word_ids)
    f1 = (2 * precision * recall) / (precision + recall)
    return f1


T = TypeVar("T")


def batchify(seq: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    seq_len = len(seq)
    for ndx in range(0, seq_len, size):
        yield seq[ndx : min(ndx + size, seq_len)]


def increasing_mask(
    batch_size: int, seq_len: int, start_with_zero: bool = False
) -> torch.Tensor:
    """
    Return an increasing mask. Useful for tests. For example:
    batch_size = 3, seq_len = 2, start_with_zero = False
    [
        [1, 0],
        [1, 1],
        [1, 1],
    ]

    batch_size = 3, seq_len = 2, start_with_zero = True
    [
        [0, 0],
        [1, 0],
        [1, 1],
    ]
    """
    data: List[List[int]] = []
    for i in range(batch_size):
        if i < seq_len:
            if start_with_zero:
                data.append([1] * i + [0] * (seq_len - i))
            else:
                data.append([1] * (i + 1) + [0] * (seq_len - i - 1))
        else:
            data.append([1] * seq_len)
    return torch.tensor(data, dtype=torch.float)


def load_textworld_games(
    game_files: List[str],
    name: str,
    request_infos: EnvInfos,
    max_episode_steps: int,
    batch_size: int,
) -> gym.Env:
    env_id = textworld.gym.register_games(
        game_files,
        request_infos=request_infos,
        batch_size=batch_size,
        max_episode_steps=max_episode_steps,
        name=name,
        asynchronous=False,
    )
    return gym.make(env_id)
