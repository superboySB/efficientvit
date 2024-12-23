import argparse
import os
import sys
import warnings
from functools import partial

import torch
import torch.nn as nn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(BASE_DIR))))
sys.path.append(ROOT_DIR)

from efficientvit.models.efficientvit.sam import EfficientViTSam
from efficientvit.sam_model_zoo import create_efficientvit_sam_model

def sam_predict_masks_monkey_patch(
        self,
        image_embeddings: torch.Tensor,
        image_pe: torch.Tensor,
        sparse_prompt_embeddings: torch.Tensor,
        dense_prompt_embeddings: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
    """
    xuanlin: Seems that torch.repeat_interleave has issues in onnx -> tensorrt conversion, so I replaced it with torch.tile.
    """
    
    # Concatenate output tokens
    output_tokens = torch.cat([self.iou_token.weight, self.mask_tokens.weight], dim=0)
    output_tokens = output_tokens.unsqueeze(0).expand(sparse_prompt_embeddings.size(0), -1, -1)
    tokens = torch.cat((output_tokens, sparse_prompt_embeddings), dim=1)
    # Expand per-image data in batch direction to be per-mask
    # src = torch.repeat_interleave(image_embeddings, tokens.shape[0], dim=0)
    src = torch.tile(image_embeddings, (tokens.shape[0], 1, 1, 1))
    src = src + dense_prompt_embeddings
    # pos_src = torch.repeat_interleave(image_pe, tokens.shape[0], dim=0)
    pos_src = torch.tile(image_pe, (tokens.shape[0], 1, 1, 1))
    b, c, h, w = src.shape
    # Run the transformer
    hs, src = self.transformer(src, pos_src, tokens)
    iou_token_out = hs[:, 0, :]
    mask_tokens_out = hs[:, 1 : (1 + self.num_mask_tokens), :]
    # Upscale mask embeddings and predict masks using the mask tokens
    src = src.transpose(1, 2).view(b, c, h, w)
    upscaled_embedding = self.output_upscaling(src)
    hyper_in_list = []
    for i in range(self.num_mask_tokens):
        hyper_in_list.append(self.output_hypernetworks_mlps[i](mask_tokens_out[:, i, :]))
    hyper_in = torch.stack(hyper_in_list, dim=1)
    b, c, h, w = upscaled_embedding.shape
    masks = (hyper_in @ upscaled_embedding.view(b, c, h * w)).view(b, -1, h, w)
    # Generate mask quality predictions
    iou_pred = self.iou_prediction_head(iou_token_out)
    return masks, iou_pred
    
class DecoderOnnxModel(nn.Module):
    """
    Modified from https://github.com/facebookresearch/segment-anything/blob/main/segment_anything/utils/onnx.py.
    """

    def __init__(self, model: EfficientViTSam, return_single_mask: bool) -> None:
        super().__init__()
        self.model = model
        self.model.mask_decoder.predict_masks = partial(sam_predict_masks_monkey_patch, self.model.mask_decoder)
        self.mask_decoder = model.mask_decoder
        self.img_size = model.image_size[0]
        self.return_single_mask = return_single_mask

    @staticmethod
    def resize_longest_image_size(input_image_size: torch.Tensor, longest_side: int) -> torch.Tensor:
        input_image_size = input_image_size.to(torch.float32)
        scale = longest_side / torch.max(input_image_size)
        transformed_size = scale * input_image_size
        transformed_size = torch.floor(transformed_size + 0.5).to(torch.int64)
        return transformed_size

    def _embed_points(self, point_coords: torch.Tensor, point_labels: torch.Tensor) -> torch.Tensor:
        point_coords = point_coords + 0.5
        point_coords = point_coords / self.img_size
        point_embedding = self.model.prompt_encoder.pe_layer._pe_encoding(point_coords)
        point_labels = point_labels.unsqueeze(-1).expand_as(point_embedding)

        point_embedding = point_embedding * (point_labels != -1)
        point_embedding = point_embedding + self.model.prompt_encoder.not_a_point_embed.weight * (point_labels == -1)

        for i in range(self.model.prompt_encoder.num_point_embeddings):
            point_embedding = point_embedding + self.model.prompt_encoder.point_embeddings[i].weight * (
                point_labels == i
            )

        return point_embedding

    def select_masks(
        self, masks: torch.Tensor, iou_preds: torch.Tensor, num_points: int
    ) -> tuple[torch.Tensor, torch.Tensor]:
        best_idx = torch.argmax(iou_preds, dim=1)
        masks = masks[torch.arange(masks.shape[0]), best_idx, :, :].unsqueeze(1)
        iou_preds = iou_preds[torch.arange(masks.shape[0]), best_idx].unsqueeze(1)

        return masks, iou_preds

    @torch.no_grad()
    def forward(
        self,
        image_embeddings: torch.Tensor,
        point_coords: torch.Tensor,
        point_labels: torch.Tensor,
    ):
        sparse_embedding = self._embed_points(point_coords, point_labels)
        dense_embedding = self.model.prompt_encoder.no_mask_embed.weight.reshape(1, -1, 1, 1).expand(
            image_embeddings.shape[0], -1, image_embeddings.shape[-2], image_embeddings.shape[-1]
        )

        masks, scores = self.model.mask_decoder.predict_masks(
            image_embeddings=image_embeddings,
            image_pe=self.model.prompt_encoder.get_dense_pe(),
            sparse_prompt_embeddings=sparse_embedding,
            dense_prompt_embeddings=dense_embedding,
        )

        if self.return_single_mask:
            masks, scores = self.select_masks(masks, scores, point_coords.shape[1])

        return masks, scores


def run_export(
    model: str,
    weight_url: str,
    output: str,
    opset: int,
    return_single_mask: bool,
):
    print("Loading model...")
    efficientvit_sam = create_efficientvit_sam_model(model, True, weight_url).eval()

    onnx_model = DecoderOnnxModel(
        model=efficientvit_sam,
        return_single_mask=return_single_mask,
    )

    dynamic_axes = {
        "point_coords": {0: "batch_size", 1: "num_points"},
        "point_labels": {0: "batch_size", 1: "num_points"},
    }

    embed_dim = efficientvit_sam.prompt_encoder.embed_dim
    embed_size = efficientvit_sam.prompt_encoder.image_embedding_size
    dummy_inputs = {
        "image_embeddings": torch.randn(1, embed_dim, *embed_size, dtype=torch.float),
        "point_coords": torch.randint(low=0, high=1024, size=(16, 2, 2), dtype=torch.float),
        "point_labels": torch.randint(low=0, high=4, size=(16, 2), dtype=torch.float),
    }

    _ = onnx_model(**dummy_inputs)

    output_names = ["masks", "iou_predictions"]

    if not os.path.exists(os.path.dirname(output)):
        os.makedirs(os.path.dirname(output))

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=torch.jit.TracerWarning)
        warnings.filterwarnings("ignore", category=UserWarning)
        with open(output, "wb") as f:
            print(f"Exporting onnx model to {output}...")
            torch.onnx.export(
                onnx_model,
                tuple(dummy_inputs.values()),
                f,
                export_params=True,
                verbose=False,
                opset_version=opset,
                do_constant_folding=True,
                input_names=list(dummy_inputs.keys()),
                output_names=output_names,
                dynamic_axes=dynamic_axes,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str)
    parser.add_argument("--weight_url", type=str)
    parser.add_argument("--output", type=str, required=True, help="The filename to save the onnx model to.")
    parser.add_argument("--opset", type=int, default=17, help="The ONNX opset version to use. Must be >=11.")
    parser.add_argument(
        "--return-single-mask",
        action="store_true",
        help=(
            "If true, the exported ONNX model will only return the best mask, "
            "instead of returning multiple masks. For high resolution images "
            "this can improve runtime when upscaling masks is expensive."
        ),
    )
    args = parser.parse_args()
    run_export(
        model=args.model,
        weight_url=args.weight_url,
        output=args.output,
        opset=args.opset,
        return_single_mask=args.return_single_mask,
    )
