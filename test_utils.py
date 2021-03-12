import torch
import pytest
import torch.nn.functional as F

from preprocessor import SpacyPreprocessor, PAD, UNK
from utils import (
    load_fasttext,
    masked_mean,
    generate_square_subsequent_mask,
    masked_softmax,
    calculate_seq_f1,
    batchify,
)


def test_load_fasttext():
    preprocessor = SpacyPreprocessor([PAD, UNK, "my", "name", "is", "peter"])
    emb = load_fasttext("test-data/test-fasttext.vec", preprocessor)
    word_ids, _ = preprocessor.preprocess_tokenized(
        [
            ["hi", "there", "what's", "your", "name"],
            ["my", "name", "is", "peter"],
        ]
    )
    embedded = emb(word_ids)
    # OOVs
    assert embedded[0, :4].equal(
        emb(torch.tensor(preprocessor.unk_id)).unsqueeze(0).expand(4, -1)
    )
    # name
    assert embedded[0, 4].equal(emb(torch.tensor(3)))
    # my name is peter
    assert embedded[1, :4].equal(emb(torch.tensor([2, 3, 4, 5])))
    # pad, should be zero
    assert embedded[1, 4].equal(torch.zeros(300))


def test_masked_mean():
    batched_input = torch.tensor(
        [
            [
                [1, 2, 300],
                [300, 100, 200],
                [3, 4, 100],
            ],
            [
                [300, 100, 200],
                [6, 2, 300],
                [10, 4, 100],
            ],
        ]
    ).float()
    batched_mask = torch.tensor(
        [
            [1, 0, 1],
            [0, 1, 1],
        ]
    ).float()
    assert masked_mean(batched_input, batched_mask).equal(
        torch.tensor(
            [
                [2, 3, 200],
                [8, 3, 200],
            ]
        ).float()
    )


def test_masked_softmax():
    batched_input = torch.tensor([[1, 2, 3], [1, 1, 2], [3, 2, 1]]).float()
    batched_mask = torch.tensor([[1, 1, 0], [0, 1, 1], [1, 1, 1]]).float()
    batched_output = masked_softmax(batched_input, batched_mask, dim=1)

    # compare the result from masked_softmax with regular softmax with filtered values
    for input, mask, output in zip(batched_input, batched_mask, batched_output):
        assert output[output != 0].equal(F.softmax(input[mask == 1], dim=0))


@pytest.mark.parametrize("size", [1, 3, 5, 7])
def test_generate_subsequent_mask(size):
    mask = generate_square_subsequent_mask(size)
    # assert that the sum of tril and triu is the original mask
    assert mask.equal(torch.tril(mask) + torch.triu(mask, diagonal=1))


@pytest.mark.parametrize(
    "preds,groundtruth,expected",
    [
        ([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], 1.0),
        ([1, 2, 3, 4, 5], [5, 4, 3, 2, 1], 1.0),
        ([1, 2, 3], [1, 2], 0.8),
        ([1, 2, 3], [5, 4], 0.0),
    ],
)
def test_calculate_seq_f1(preds, groundtruth, expected):
    assert calculate_seq_f1(preds, groundtruth) == expected


@pytest.mark.parametrize(
    "seq,size,batches",
    [
        ([1, 2, 3, 4, 5, 6], 3, [[1, 2, 3], [4, 5, 6]]),
        ([1, 2, 3, 4, 5, 6, 7, 8], 3, [[1, 2, 3], [4, 5, 6], [7, 8]]),
    ],
)
def test_batchify(seq, size, batches):
    assert list(batchify(seq, size)) == batches
