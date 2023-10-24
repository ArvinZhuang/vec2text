from typing import Tuple

import torch
import torch.nn as nn
import transformers

from vec2text.models.config import InversionConfig

from .corrector_encoder import CorrectorEncoderModel


class CorrectorEncoderFromLogitsModel(CorrectorEncoderModel):
    config_class = InversionConfig
    encoder_decoder: transformers.PreTrainedModel

    def __init__(
        self,
        config: InversionConfig,
        embedder_dim: int,
        num_repeat_tokens: int,
    ):
        super().__init__(config=config)

        self.embedder_dim = embedder_dim
        self.num_repeat_tokens = num_repeat_tokens

        bottleneck_dim = embedder_dim

        self.sequence_weights_1 = nn.Parameter(
            torch.randn(
                (self.num_repeat_tokens, self.embedder_dim, self.embedder_dim),
                dtype=torch.float32,
            ),
            requires_grad=True,
        )
        self.sequence_weights_2 = nn.Parameter(
            torch.randn(
                (self.num_repeat_tokens, self.embedder_dim, self.embedder_dim),
                dtype=torch.float32,
            ),
            requires_grad=True,
        )
        self.sequence_weights_3 = nn.Parameter(
            torch.randn(
                (self.num_repeat_tokens, self.embedder_dim, self.embedder_dim),
                dtype=torch.float32,
            ),
            requires_grad=True,
        )

        self.embedding_transform_1 = nn.Sequential(
            nn.Linear(self.embedder_dim, bottleneck_dim),
            nn.Dropout(
                self.encoder_decoder.config.dropout_rate if self.use_ff_dropout else 0.0
            ),
            nn.GELU(),
            nn.Linear(bottleneck_dim, self.encoder_hidden_dim),
        )
        self.embedding_transform_2 = nn.Sequential(
            nn.Linear(self.embedder_dim, bottleneck_dim),
            nn.Dropout(
                self.encoder_decoder.config.dropout_rate if self.use_ff_dropout else 0.0
            ),
            nn.GELU(),
            nn.Linear(bottleneck_dim, self.encoder_hidden_dim),
        )
        self.embedding_transform_3 = nn.Sequential(
            nn.Linear(self.embedder_dim, bottleneck_dim),
            nn.Dropout(
                self.encoder_decoder.config.dropout_rate if self.use_ff_dropout else 0.0
            ),
            nn.GELU(),
            nn.Linear(bottleneck_dim, self.encoder_hidden_dim),
        )

    def get_encoder_embedding(
        self,
        embedding: torch.Tensor,
        hypothesis_embedding: torch.Tensor,
        hypothesis_input_ids: torch.Tensor,
        hypothesis_attention_mask: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        batch_size, D = embedding.shape
        if (self.training) and (self.training_embedding_noise_level > 0):
            embedding += self.training_embedding_noise_level * torch.randn(
                embedding.shape, device=embedding.device
            )
            hypothesis_embedding += self.training_embedding_noise_level * torch.randn(
                hypothesis_embedding.shape, device=hypothesis_embedding.device
            )

        if self.ignore_hypothesis_embedding:
            # For "No Feedback" ablation
            hypothesis_embedding = embedding

        diff_embedding = embedding - hypothesis_embedding

        embedding = embedding.to(self.sequence_weights_1.dtype)
        embedding = embedding.reshape(
            (embedding.shape[0], self.num_repeat_tokens, self.embedder_dim)
        )
        embedding = torch.einsum("bsd,sdw->bsw", embedding, self.sequence_weights_1)
        embedding = self.embedding_transform_1(embedding)
        #
        diff_embedding = diff_embedding.to(self.sequence_weights_2.dtype)
        diff_embedding = diff_embedding.reshape(
            (diff_embedding.shape[0], self.num_repeat_tokens, self.embedder_dim)
        )
        diff_embedding = torch.einsum(
            "bsd,sdw->bsw", diff_embedding, self.sequence_weights_2
        )
        diff_embedding = self.embedding_transform_2(diff_embedding)
        #
        hypothesis_embedding = hypothesis_embedding.to(self.sequence_weights_3.dtype)
        hypothesis_embedding = hypothesis_embedding.reshape(
            (hypothesis_embedding.shape[0], self.num_repeat_tokens, self.embedder_dim)
        )
        hypothesis_embedding = torch.einsum(
            "bsd,sdw->bsw", hypothesis_embedding, self.sequence_weights_3
        )
        hypothesis_embedding = self.embedding_transform_3(hypothesis_embedding)
        inputs_embeds = self.encoder_decoder.encoder.embed_tokens(hypothesis_input_ids)
        #
        ones = torch.ones(
            (batch_size, 1), dtype=torch.long, device=hypothesis_input_ids.device
        )
        sep_token = ones * self.encoder_decoder.config.eos_token_id
        sep_token = self.encoder_decoder.encoder.embed_tokens(sep_token)

        inputs_embeds = torch.cat(
            (
                sep_token,
                embedding,
                sep_token,
                hypothesis_embedding,
                sep_token,
                diff_embedding,
                sep_token,
                inputs_embeds,
            ),
            dim=1,
        )
        if self.use_ln:
            inputs_embeds = self.layernorm(inputs_embeds)
        attention_mask = torch.cat(
            (ones.repeat(1, 4 + 3 * self.num_repeat_tokens), hypothesis_attention_mask),
            dim=1,
        )
        return (inputs_embeds, attention_mask)