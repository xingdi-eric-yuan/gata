import torch
import torch.nn as nn
import pytorch_lightning as pl
import hydra
import random
import wandb

from typing import List, Dict, Tuple, Optional
from omegaconf import DictConfig, OmegaConf
from hydra.utils import instantiate, to_absolute_path
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import WandbLogger

from utils import load_fasttext, masked_mean, generate_square_subsequent_mask
from preprocessor import SpacyPreprocessor
from graph_updater import GraphUpdater, PositionalEncoderTensor2Tensor
from optimizers import RAdam
from graph_updater_data import GraphUpdaterObsGenDataModule


class TextDecoderBlock(nn.Module):
    def __init__(self, hidden_dim: int, num_heads: int) -> None:
        super().__init__()
        assert hidden_dim % 2 == 0, "hidden_dim has to be even for positional encoding"
        self.num_heads = num_heads

        self.pos_encoder = PositionalEncoderTensor2Tensor(hidden_dim, 512)
        self.self_attn = nn.MultiheadAttention(hidden_dim, num_heads)
        self.self_attn_layer_norm = nn.LayerNorm(hidden_dim)
        self.node_attn = nn.MultiheadAttention(hidden_dim, num_heads)
        self.prev_action_attn = nn.MultiheadAttention(hidden_dim, num_heads)
        self.combine_node_prev_action = nn.Sequential(
            nn.Linear(2 * hidden_dim, hidden_dim), nn.ReLU()
        )
        self.linear_layer_norm = nn.LayerNorm(hidden_dim)
        self.linear_layers = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )

    def forward(
        self,
        input: torch.Tensor,
        input_mask: torch.Tensor,
        node_hidden: torch.Tensor,
        prev_action_hidden: torch.Tensor,
        prev_action_mask: torch.Tensor,
    ) -> torch.Tensor:
        """
        input: (batch, input_seq_len, hidden_dim)
        input_mask: (batch, input_seq_len)
        node_hidden: (batch, num_node, hidden_dim)
        prev_action_hidden: (batch, prev_action_len, hidden_dim)
        prev_action_mask: (batch, prev_action_len)

        output: (batch, input_seq_len, hidden_dim)
        """
        # calculate attention mask for decoding
        # this is the mask that prevents MultiheadAttention
        # from attending to future values
        input_seq_len = input.size(1)
        attn_mask = generate_square_subsequent_mask(input_seq_len).to(input.device)
        # (input_seq_len, input_seq_len)

        # add the positional encodings
        pos_encoded_input = self.pos_encoder(input)

        # self attention layer
        input_residual = pos_encoded_input
        # MultiheadAttention expects batch dim to be 1 for q, k, v
        # but 0 for key_padding_mask, so we need to transpose
        pos_encoded_input = pos_encoded_input.transpose(0, 1)
        input_attn, _ = self.self_attn(
            pos_encoded_input,
            pos_encoded_input,
            pos_encoded_input,
            key_padding_mask=input_mask == 0,
            attn_mask=attn_mask,
        )
        input_attn = input_attn.transpose(0, 1)
        input_attn *= input_mask.unsqueeze(-1)
        input_attn += input_residual
        # (batch, input_seq_len, hidden_dim)

        # calculate self attention for the nodes and previous action
        # strictly speaking, we should calculate attention masks for these
        # based on input_mask, but due to this bug:
        # https://github.com/pytorch/pytorch/issues/41508
        # it returns nan's if we apply attention masks. So let's just skip it.
        # It's OK, b/c we apply input_mask when we combine these.
        # apply layer norm to the input self attention output to calculate the query
        query = self.self_attn_layer_norm(input_attn).transpose(0, 1)
        # (input_seq_len, batch, hidden_dim)

        # self attention for the nodes
        # no key_padding_mask, since we use all the nodes
        # (batch * num_heads, input_seq_len, num_node)
        node_hidden = node_hidden.transpose(0, 1)
        node_attn, _ = self.node_attn(query, node_hidden, node_hidden)
        node_attn = node_attn.transpose(0, 1)
        # (batch, input_seq_len, hidden_dim)

        # self attention for the previous action
        # key_padding_mask is from prev_action_mask
        # (batch * num_heads, input_seq_len, prev_action_len)
        prev_action_hidden = prev_action_hidden.transpose(0, 1)
        prev_action_attn, _ = self.prev_action_attn(
            query,
            prev_action_hidden,
            prev_action_hidden,
            key_padding_mask=prev_action_mask == 0,
        )
        prev_action_attn = prev_action_attn.transpose(0, 1)
        # (batch, input_seq_len, hidden_dim)

        # combine self attention for the previous action and nodes with
        # input self attention
        combined_self_attn = self.combine_node_prev_action(
            torch.cat([prev_action_attn, node_attn], dim=-1)
        )
        combined_self_attn *= input_mask.unsqueeze(-1)
        combined_self_attn += input_attn
        # (batch, input_seq_len, hidden_dim)

        # linear layer
        output = self.linear_layer_norm(combined_self_attn)
        output = self.linear_layers(output)
        output += combined_self_attn
        # (batch, input_seq_len, hidden_dim)

        return output


class TextDecoder(nn.Module):
    def __init__(
        self, num_dec_blocks: int, dec_block_hidden_dim: int, dec_block_num_heads: int
    ) -> None:
        super().__init__()
        self.dec_blocks = nn.ModuleList(
            TextDecoderBlock(dec_block_hidden_dim, dec_block_num_heads)
            for _ in range(num_dec_blocks)
        )

    def forward(
        self,
        input: torch.Tensor,
        input_mask: torch.Tensor,
        node_hidden: torch.Tensor,
        prev_action_hidden: torch.Tensor,
        prev_action_mask: torch.Tensor,
    ) -> torch.Tensor:
        """
        input: (batch, input_seq_len, hidden_dim)
        input_mask: (batch, input_seq_len)
        node_hidden: (batch, num_node, hidden_dim)
        prev_action_hidden: (batch, prev_action_len, hidden_dim)
        prev_action_mask: (batch, prev_action_len)

        output: (batch, input_seq_len, hidden_dim)
        """
        # (batch_size, input_seq_len, hidden_dim)
        output = input
        for dec_block in self.dec_blocks:
            output = dec_block(
                output, input_mask, node_hidden, prev_action_hidden, prev_action_mask
            )
        # (batch_size, input_seq_len, hidden_dim)

        return output


class GraphUpdaterObsGen(pl.LightningModule):
    def __init__(
        self,
        hidden_dim: int,
        pretrained_word_embedding_path: str,
        word_emb_dim: int,
        node_vocab_path: str,
        node_emb_dim: int,
        relation_vocab_path: str,
        relation_emb_dim: int,
        text_encoder_num_blocks: int,
        text_encoder_num_conv_layers: int,
        text_encoder_kernel_size: int,
        text_encoder_num_heads: int,
        graph_encoder_num_cov_layers: int,
        graph_encoder_num_bases: int,
        text_decoder_num_blocks: int,
        text_decoder_num_heads: int,
        learning_rate: float,
        sample_k_gen_obs: int,
        preprocessor: SpacyPreprocessor,
    ) -> None:
        super().__init__()
        self.save_hyperparameters()

        # preprocessor
        self.preprocessor = preprocessor

        # sample k generated observations in val and test
        self.sample_k_gen_obs = sample_k_gen_obs

        # load pretrained word embedding and freeze it
        pretrained_word_embedding = load_fasttext(
            to_absolute_path(pretrained_word_embedding_path), self.preprocessor
        )
        pretrained_word_embedding.weight.requires_grad = False

        # load node vocab
        with open(to_absolute_path(node_vocab_path), "r") as f:
            self.node_vocab = [node_name.strip() for node_name in f]

        # load relation vocab
        with open(to_absolute_path(relation_vocab_path), "r") as f:
            self.relation_vocab = [relation_name.strip() for relation_name in f]
        # add reverse relations
        self.relation_vocab += [rel + " reverse" for rel in self.relation_vocab]

        # calculate mean masked node name embeddings
        node_name_word_ids, node_name_mask = self.preprocessor.preprocess(
            self.node_vocab
        )
        node_name_embeddings = masked_mean(
            pretrained_word_embedding(node_name_word_ids), node_name_mask
        )
        rel_name_word_ids, rel_name_mask = self.preprocessor.preprocess(
            self.relation_vocab
        )
        rel_name_embeddings = masked_mean(
            pretrained_word_embedding(rel_name_word_ids), rel_name_mask
        )

        # graph updater
        self.graph_updater = GraphUpdater(
            hidden_dim,
            word_emb_dim,
            len(self.node_vocab),
            node_emb_dim,
            len(self.relation_vocab),
            relation_emb_dim,
            text_encoder_num_blocks,
            text_encoder_num_conv_layers,
            text_encoder_kernel_size,
            text_encoder_num_heads,
            graph_encoder_num_cov_layers,
            graph_encoder_num_bases,
            pretrained_word_embedding,
            node_name_embeddings,
            rel_name_embeddings,
        )
        self.graph_updater.pretraining = True

        # text decoder
        self.text_decoder = TextDecoder(
            text_decoder_num_blocks, hidden_dim, text_decoder_num_heads
        )
        self.target_word_prj = nn.Linear(
            hidden_dim, len(self.preprocessor.word_to_id_dict), bias=False
        )
        self.ce_loss = nn.CrossEntropyLoss(
            ignore_index=self.preprocessor.pad_id, reduction="none"
        )

    def forward(  # type: ignore
        self,
        episode_data: Dict[str, torch.Tensor],
        rnn_prev_hidden: Optional[torch.Tensor] = None,
    ) -> Dict[str, torch.Tensor]:
        """
        episode_data:
        {
            'obs_word_ids': tensor of shape (batch, obs_len),
            'obs_mask': tensor of shape (batch, obs_len),
            'prev_action_word_ids': tensor of shape (batch, prev_action_len),
            'prev_action_mask': tensor of shape (batch, prev_action_len),
            'groundtruth_obs_word_ids': tensor of shape (batch, obs_len),
        }
        rnn_prev_hidden: (batch, hidden_dim)

        output:
        {
            'h_t': hidden state of the rnn cell at time t; (batch, hidden_dim),
            'batch_loss': batch loss for this episode data. (batch)
            'pred_obs_word_ids': predicted observation word IDs.
                (batch, obs_len),
        }
        """
        # graph updater
        graph_updater_results = self.graph_updater(
            episode_data["obs_word_ids"],
            episode_data["prev_action_word_ids"],
            episode_data["obs_mask"],
            episode_data["prev_action_mask"],
            rnn_prev_hidden=rnn_prev_hidden,
        )

        # decode
        decoder_output = self.text_decoder(
            graph_updater_results["prj_obs"],
            episode_data["obs_mask"],
            graph_updater_results["h_ga"],
            graph_updater_results["h_ag"],
            episode_data["prev_action_mask"],
        )
        # (batch, obs_len, hidden_dim)
        decoder_output = self.target_word_prj(decoder_output)
        # (batch, obs_len, num_words)

        batch_size = decoder_output.size(0)
        batch_loss = (
            self.ce_loss(
                decoder_output.view(-1, decoder_output.size(-1)),
                episode_data["groundtruth_obs_word_ids"].flatten(),
            )
            .view(batch_size, -1)
            .sum(dim=1)
        )
        # (batch)

        pred_obs_word_ids = (
            decoder_output
            * (episode_data["groundtruth_obs_word_ids"] != 0).float().unsqueeze(-1)
        ).argmax(dim=-1)
        # (batch, obs_len)

        return {
            "h_t": graph_updater_results["h_t"],
            "batch_loss": batch_loss,
            "pred_obs_word_ids": pred_obs_word_ids,
        }

    def process_batch(
        self, batch: Tuple[List[Dict[str, torch.Tensor]], torch.Tensor], batch_idx: int
    ) -> Tuple[List[torch.Tensor], List[Dict[str, torch.Tensor]]]:
        episode_seq, episode_mask = batch
        h_t: Optional[torch.Tensor] = None
        losses: List[torch.Tensor] = []
        episode_results: List[Dict[str, torch.Tensor]] = []
        for i, episode_data in enumerate(episode_seq):
            results = self(episode_data, rnn_prev_hidden=h_t)
            episode_results.append(results)
            h_t = results["h_t"]
            loss_mask = episode_mask[:, i]
            losses.append(
                (
                    torch.sum(results["batch_loss"] * loss_mask) / loss_mask.sum()
                ).unsqueeze(0)
            )

        return losses, episode_results

    def training_step(  # type: ignore
        self, batch: Tuple[List[Dict[str, torch.Tensor]], torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        losses, _ = self.process_batch(batch, batch_idx)
        return torch.cat(losses).mean()

    def validation_step(  # type: ignore
        self, batch: Tuple[List[Dict[str, torch.Tensor]], torch.Tensor], batch_idx: int
    ) -> List[List[str]]:
        losses, episode_results = self.process_batch(batch, batch_idx)
        self.log("val_loss", torch.cat(losses).mean())

        # decode all the predicted observations
        episode_seq, _ = batch
        groundtruth_obs_word_ids = [
            word_ids
            for episode in episode_seq
            for word_ids in episode["groundtruth_obs_word_ids"].tolist()
        ]
        pred_obs_word_ids = [
            word_ids
            for result in episode_results
            for word_ids in result["pred_obs_word_ids"].tolist()
        ]
        return [
            [groundtruth, generated]
            for groundtruth, generated in zip(
                [
                    obs
                    for obs in self.preprocessor.decode(groundtruth_obs_word_ids)
                    if len(obs.split()) > 1
                ],
                [
                    obs
                    for obs in self.preprocessor.decode(pred_obs_word_ids)
                    if len(obs.split()) > 1
                ],
            )
        ]

    def wandb_log_gen_obs(
        self, outputs: List[List[List[str]]], table_title: str
    ) -> None:
        flat_outputs = [item for sublist in outputs for item in sublist]
        data = (
            random.sample(flat_outputs, self.sample_k_gen_obs)
            if len(flat_outputs) >= self.sample_k_gen_obs
            else flat_outputs
        )
        self.logger.experiment.log(
            {table_title: wandb.Table(data=data, columns=["Groundtruth", "Generated"])}
        )

    def validation_epoch_end(self, outputs: List[List[List[str]]]) -> None:
        if isinstance(self.logger, WandbLogger):
            self.wandb_log_gen_obs(
                outputs, f"Generated Observations Val Epoch {self.current_epoch}"
            )

    def test_step(  # type: ignore
        self, batch: Tuple[List[Dict[str, torch.Tensor]], torch.Tensor], batch_idx: int
    ) -> List[List[str]]:
        return self.validation_step(batch, batch_idx)

    def test_epoch_end(self, outputs: List[List[List[str]]]) -> None:
        if isinstance(self.logger, WandbLogger):
            self.wandb_log_gen_obs(
                outputs, f"Generated Observations Test Epoch {self.current_epoch}"
            )

    def configure_optimizers(self):
        return RAdam(self.parameters(), lr=self.hparams.learning_rate)


@hydra.main(config_path="train_graph_updater_conf", config_name="config")
def main(cfg: DictConfig) -> None:
    print(f"Training with the following config:\n{OmegaConf.to_yaml(cfg)}")

    # seed
    pl.seed_everything(42)

    # set up data module
    dm = GraphUpdaterObsGenDataModule(**cfg.data)

    # instantiate the lightning module
    lm = GraphUpdaterObsGen(**cfg.model, **cfg.train, preprocessor=dm.preprocessor)

    # trainer
    trainer_config = OmegaConf.to_container(cfg.pl_trainer, resolve=True)
    assert isinstance(trainer_config, dict)
    trainer_config["logger"] = instantiate(cfg.logger) if "logger" in cfg else True
    trainer = pl.Trainer(
        **trainer_config,
        checkpoint_callback=ModelCheckpoint(monitor="val_loss", mode="min"),
    )

    # fit
    trainer.fit(lm, datamodule=dm)

    # test
    if cfg.train.run_test:
        trainer.test(datamodule=dm)


if __name__ == "__main__":
    main()
