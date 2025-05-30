# copyright (c) 2019 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
from typing import Dict, List, Any
import paddle
from paddle import nn


class GuidanceLoss(nn.Layer):
    def __init__(self, mappings_path: str, **kwargs):
        super(GuidanceLoss, self).__init__()
        self.mappings = self._load_mappings(mappings_path)
        # Pre-compute max vocab size for efficiency
        self.max_vocab_idx = (
            max(
                max(char_indices)
                for char_indices in self.mappings.values()
                if char_indices
            )
            if self.mappings
            else 0
        )

    def _load_mappings(self, mappings_path: str) -> Dict[str, List[int]]:
        with open(mappings_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _create_target_distributions_batch(
        self, translations, masks, vocab_size
    ) -> paddle.Tensor:
        all_target_dists = []

        for translation, mask in zip(translations, masks):
            # Convert binary mask to indices where mask == 1
            if isinstance(mask, paddle.Tensor):
                mask_indices = paddle.nonzero(mask).flatten().numpy().tolist()
            else:
                mask_indices = [i for i, val in enumerate(mask) if val == 1]

            if len(mask_indices) == 0:
                continue

            # Create target distribution for this sample
            sample_target_dist = paddle.zeros([len(mask_indices), vocab_size])

            for i, idx in enumerate(mask_indices):
                if idx < len(translation):
                    mapped_chars = self.mappings.get(translation[idx], [])
                    if mapped_chars:
                        # Filter valid character indices
                        valid_chars = [
                            char_idx
                            for char_idx in mapped_chars
                            if char_idx < vocab_size
                        ]
                        if valid_chars:
                            uniform_prob = 1.0 / len(valid_chars)
                            sample_target_dist[i, valid_chars] = uniform_prob

            all_target_dists.append(sample_target_dist)

        if all_target_dists:
            return paddle.concat(all_target_dists, axis=0)
        else:
            return paddle.zeros([0, vocab_size])

    def _extract_predictions_batch(
        self, predicts: paddle.Tensor, masks: List
    ) -> paddle.Tensor:
        all_pred_subsets = []

        for batch_idx, mask in enumerate(masks):
            # Convert binary mask to indices where mask == 1
            if isinstance(mask, paddle.Tensor):
                mask_indices = paddle.nonzero(mask).flatten().numpy().tolist()
            else:
                mask_indices = [i for i, val in enumerate(mask) if val == 1]

            if len(mask_indices) == 0:
                continue

            # Extract predictions for masked positions in this batch element
            # predicts shape: [N, B, vocab_size], we need [mask_positions, vocab_size]
            pred_subset = predicts[
                mask_indices, batch_idx, :
            ]  # Shape: [len(mask_indices), vocab_size]
            all_pred_subsets.append(pred_subset)

        if all_pred_subsets:
            return paddle.concat(all_pred_subsets, axis=0)
        else:
            return paddle.zeros([0, predicts.shape[2]])

    def _compute_kl_divergence(
        self, log_probs: paddle.Tensor, target_probs: paddle.Tensor
    ) -> paddle.Tensor:
        """Compute KL divergence between log probabilities and target probabilities."""
        eps = 1.0e-10
        loss = target_probs * (paddle.log(target_probs + eps) - log_probs)
        return paddle.sum(loss)  # Sum over all dimensions

    def forward(
        self, predicts: Any, batch: List[paddle.Tensor]
    ) -> Dict[str, paddle.Tensor]:
        """
        Forward pass of guidance loss.

        Args:
            predicts: Model predictions
            batch: Batch data containing labels, lengths, masks, and translations

        Returns:
            Dictionary containing the computed loss
        """
        # Handle different predict formats
        if isinstance(predicts, (list, tuple)):
            predicts = predicts[-1]

        # Transpose to [N, B, vocab_size] format
        predicts = predicts.transpose((1, 0, 2))
        N, B, vocab_size = predicts.shape

        # Extract batch components
        label_masks = batch[-2]  # List of binary masks
        label_translations = batch[-1]

        # Ensure masks are properly formatted
        processed_masks = []
        for mask in label_masks:
            if isinstance(mask, paddle.Tensor):
                # Ensure mask is on CPU for numpy conversion
                if mask.place.is_gpu_place():
                    mask = mask.cpu()
                processed_masks.append(mask)
            else:
                processed_masks.append(mask)

        # Count total valid samples and positions
        valid_samples = 0
        total_positions = 0

        for mask in processed_masks:
            if isinstance(mask, paddle.Tensor):
                mask_sum = int(paddle.sum(mask).numpy())
                if mask_sum > 0:
                    valid_samples += 1
                    total_positions += mask_sum
            else:
                mask_sum = sum(mask)
                if mask_sum > 0:
                    valid_samples += 1
                    total_positions += mask_sum

        if total_positions == 0:
            # No valid positions to compute loss
            return {"loss": paddle.zeros([1])}

        # Batch create target distributions
        target_dists = self._create_target_distributions_batch(
            label_translations, processed_masks, vocab_size
        )

        # Batch extract predictions
        pred_subsets = self._extract_predictions_batch(predicts, processed_masks)

        if target_dists.shape[0] == 0 or pred_subsets.shape[0] == 0:
            return {"loss": paddle.zeros([1])}

        # Apply log softmax to predictions
        pred_log_probs = paddle.nn.functional.log_softmax(pred_subsets, axis=-1)

        # Compute KL divergence loss in batch
        loss = self._compute_kl_divergence(pred_log_probs, target_dists)

        # Normalize by number of valid samples
        if valid_samples > 0:
            loss = loss / valid_samples

        return {"loss": loss}


def test_guidance_loss():
    """Test the GuidanceLoss class functionality."""
    import os

    mappings_path = "ppocr/utils/context/candidate_mappings.json"
    vocab_path = "ppocr/utils/dict/PP-Thesis/hisdoc1b_ss1_nomnaocr.txt"

    # Create dummy mappings for testing if file doesn't exist
    dummy_mappings = {
        "kịn": [0, 1, 2],
        "kinh": [3, 4, 5],
    }

    # Read vocabulary from file or create dummy vocab
    vocab = []
    if os.path.exists(vocab_path):
        with open(vocab_path, "r", encoding="utf-8") as f:
            vocab = [line.strip() for line in f.readlines()]
    else:
        vocab = [str(i) for i in range(100)]  # Dummy vocab for testing

    # Create dummy mappings file if it doesn't exist
    if not os.path.exists(mappings_path):
        os.makedirs(os.path.dirname(mappings_path), exist_ok=True)
        with open(mappings_path, "w", encoding="utf-8") as f:
            json.dump(dummy_mappings, f)

    try:
        # Initialize the loss
        loss_fn = GuidanceLoss(mappings_path)

        # Test parameters
        batch_size = 2
        seq_len = 4
        vocab_size = len(vocab)

        # Create mock predictions (B, N, vocab_size)
        predicts = paddle.randn([batch_size, seq_len, vocab_size])

        # Create mock batch data
        labels = paddle.to_tensor([[0, 1, 2, 3], [4, 5, 6, 7]], dtype="int64")  # [B, N]
        lengths = paddle.to_tensor([4, 4], dtype="int32")  # [B]
        label_indices = paddle.to_tensor([[0, 1], [0, 1]], dtype="int64")  # [B, 2]

        # Binary masks where 1 indicates positions to apply guidance
        label_masks = [
            paddle.to_tensor([1, 1, 0, 0], dtype="int64"),  # positions 0,1 are masked
            paddle.to_tensor([0, 0, 1, 1], dtype="int64"),  # positions 2,3 are masked
        ]

        label_translations = [
            ["kịn", "kinh", "test", "test"],
            ["test", "test", "kinh", "kịn"],
        ]  # corresponding characters

        batch = [labels, lengths, label_indices, label_masks, label_translations]

        # Test forward pass
        result = loss_fn(predicts, batch)

        # Verify output
        assert isinstance(result, dict), "Output should be a dictionary"
        assert "loss" in result, "Output should contain 'loss' key"
        assert isinstance(result["loss"], paddle.Tensor), "Loss should be a tensor"

        print("Test 1 passed: Basic forward pass")
        print(f"Loss value: {result['loss'].numpy()}")

        # Test with empty masks (all zeros)
        empty_masks = [
            paddle.to_tensor([0, 0, 0, 0], dtype="int64"),
            paddle.to_tensor([0, 0, 0, 0], dtype="int64"),
        ]
        batch_empty = [labels, lengths, label_indices, empty_masks, label_translations]
        result_empty = loss_fn(predicts, batch_empty)

        assert result_empty["loss"].numpy()[0] == 0.0, (
            "Loss should be zero for empty masks"
        )
        print("Test 2 passed: Empty masks handling")

        # Test with single sample
        single_predicts = paddle.randn([1, seq_len, vocab_size])
        single_batch = [
            labels[:1],
            lengths[:1],
            label_indices[:1],
            [label_masks[0]],
            [label_translations[0]],
        ]
        result_single = loss_fn(single_predicts, single_batch)

        assert isinstance(result_single["loss"], paddle.Tensor), (
            "Single sample should work"
        )
        print("Test 3 passed: Single sample handling")
        print(f"Single sample loss: {result_single['loss'].numpy()}")

        print("All tests passed!")

    except Exception as e:
        print(f"Test failed with error: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # Clean up dummy file if created
        if not os.path.exists(
            "ppocr/utils/context/candidate_mappings.json"
        ) and os.path.exists(mappings_path):
            os.remove(mappings_path)


if __name__ == "__main__":
    test_guidance_loss()
