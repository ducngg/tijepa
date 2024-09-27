import torch
import random

from dataclasses import dataclass, asdict

from src.models.modules import text_encoder_model, vision_encoder, crosser_module, vit_predictor
from src.utils.tensors import apply_masks, repeat_interleave_batch
from create_dataset import ImageTextDataset
from src.masks.multiblock import MaskCollator
import torch.nn.functional as F
import torch.optim as optim
from torchvision import transforms
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm
import torch.nn as nn

from src.utils.visualizer import visualize_rectangle
from src.utils.saving import Saver

##################
@dataclass
class ModelConfig:
    SIZE: int = 224
    PATCH_SIZE: int = 16

    EMBED_DIM: int = 768
    PREDICTOR_EMBED_DIM: int = 384

    DROP_RATE: float = 0.15
    ATTN_DROP_RATE: float = 0.15
    MLP_RATIO: float = 4.0

    ENCODER_ATTN_DEPTH: int = 10
    PRED_ATTN_DEPTH: int = 12
    CROSS_ATTN_DEPTH: int = 6

    ENCODER_NUM_HEADS: int = 8
    PRED_NUM_HEADS: int = 8
    CROSS_NUM_HEADS: int = 8

MODEL_CONFIG = ModelConfig()
##################


def cross_entropy(preds, targets, reduction='none'):
    log_softmax = torch.nn.LogSoftmax(dim=-1)
    loss = (-targets * log_softmax(preds)).sum(1)
    if reduction == "none":
        return loss
    elif reduction == "mean":
        return loss.mean()
    
# Calculating the Loss
def contrastive_loss(text_embeddings, image_embeddings, temperature=0.07):
    logits = (text_embeddings @ image_embeddings.T) / temperature
    print(f"{logits=}")
    images_similarity = image_embeddings @ image_embeddings.T
    texts_similarity = text_embeddings @ text_embeddings.T
    targets = F.softmax(
        (images_similarity + texts_similarity) / 2 * temperature, dim=-1
    )
    # print(f"{targets=}")
    texts_loss = cross_entropy(logits, targets, reduction='none')
    # print(f"{texts_loss=}")
    images_loss = cross_entropy(logits.T, targets.T, reduction='none')
    # print(f"{images_loss=}")
    loss =  (images_loss + texts_loss) / 2.0 # shape: (batch_size)
    return loss.mean()

import torch.nn.functional as F

def clip_loss(text_embeddings, image_embeddings, temperature=1.0):
    # Normalize embeddings for cosine similarity
    text_embeddings = F.normalize(text_embeddings, p=2, dim=-1)
    image_embeddings = F.normalize(image_embeddings, p=2, dim=-1)

    # Compute logits (cosine similarity scaled by temperature)
    logits = (text_embeddings @ image_embeddings.T) / temperature
    # print(f"{logits=}")

    # Contrastive targets: identity matrix for (text, image) pairs
    targets = torch.eye(text_embeddings.shape[0], device=text_embeddings.device)

    # Calculate the contrastive loss for both texts and images
    texts_loss = F.cross_entropy(logits, targets, reduction='none')
    images_loss = F.cross_entropy(logits.T, targets.T, reduction='none')

    # Final contrastive loss
    loss = (texts_loss + images_loss) / 2.0  # shape: (batch_size)
    
    return loss.mean()  # Reduce the mean for the final loss

def contrastive_loss(anchor, positive, negative, margin=1.0):
    distance_positive = F.pairwise_distance(anchor, positive)
    distance_negative = F.pairwise_distance(anchor, negative)
    losses = torch.relu(distance_positive - distance_negative + margin)
    return losses.mean()




def train(num_epochs=1, max_images_per_epoch=10, batch_size=10, learning_rate=0.01):
    import time
    session = str(int(time.time()))

    text_encoder_context = text_encoder_model(
        device='cuda'
    )
    text_encoder_total_params = sum(p.numel() for p in text_encoder_context.parameters())
    print(f"{text_encoder_total_params=}")

    text_encoder_target = text_encoder_model(
        device='cuda'
    )
    for p in text_encoder_target.parameters():
        p.requires_grad = False
    
    context_vision_encoder = vision_encoder(
        patch_size=MODEL_CONFIG.PATCH_SIZE,
        embed_dim=MODEL_CONFIG.EMBED_DIM,
        img_size=[MODEL_CONFIG.SIZE],
        depth=MODEL_CONFIG.ENCODER_ATTN_DEPTH,
        num_heads=MODEL_CONFIG.ENCODER_NUM_HEADS,
        mlp_ratio=MODEL_CONFIG.MLP_RATIO,
        qkv_bias=True,
        qk_scale=None,
        drop_rate=MODEL_CONFIG.DROP_RATE,
        attn_drop_rate=MODEL_CONFIG.ATTN_DROP_RATE,
    ).to('cuda')
    context_vision_encoder_total_params = sum(p.numel() for p in context_vision_encoder.parameters())
    print(f"{context_vision_encoder_total_params=}")
    
    target_vision_encoder = vision_encoder(
        patch_size=MODEL_CONFIG.PATCH_SIZE,
        embed_dim=MODEL_CONFIG.EMBED_DIM,
        img_size=[MODEL_CONFIG.SIZE],
        depth=MODEL_CONFIG.ENCODER_ATTN_DEPTH,
        num_heads=MODEL_CONFIG.ENCODER_NUM_HEADS,
        mlp_ratio=MODEL_CONFIG.MLP_RATIO,
        qkv_bias=True,
        qk_scale=None,
        drop_rate=MODEL_CONFIG.DROP_RATE,
        attn_drop_rate=MODEL_CONFIG.ATTN_DROP_RATE,
    ).to('cuda')
    for p in target_vision_encoder.parameters():
        p.requires_grad = False
    target_vision_encoder_total_params = sum(p.numel() for p in target_vision_encoder.parameters())
    print(f"{target_vision_encoder_total_params=}")
    
    NUM_PATCHES = context_vision_encoder.patch_embed.num_patches

    predictor = vit_predictor(
        embed_dim=MODEL_CONFIG.EMBED_DIM,
        depth=MODEL_CONFIG.PRED_ATTN_DEPTH,
        num_heads=MODEL_CONFIG.PRED_NUM_HEADS,
        predictor_embed_dim=MODEL_CONFIG.PREDICTOR_EMBED_DIM,
        num_patches=NUM_PATCHES,
        mlp_ratio=MODEL_CONFIG.MLP_RATIO,
        qkv_bias=True,
        qk_scale=None,
        drop_rate=MODEL_CONFIG.DROP_RATE,
        attn_drop_rate=MODEL_CONFIG.ATTN_DROP_RATE,
    ).to('cuda')
    predictor_total_params = sum(p.numel() for p in predictor.parameters())
    print(f"{predictor_total_params=}")
    
    context_crosser = crosser_module(
        text_embed_dim=768,
        vision_embed_dim=MODEL_CONFIG.EMBED_DIM,
        hidden_dim=MODEL_CONFIG.EMBED_DIM,
        depth=MODEL_CONFIG.CROSS_ATTN_DEPTH,
        num_heads=MODEL_CONFIG.CROSS_NUM_HEADS,
        mlp_ratio=MODEL_CONFIG.MLP_RATIO,
        qkv_bias=True,
        qk_scale=None,
        drop_rate=MODEL_CONFIG.DROP_RATE,
        attn_drop_rate=MODEL_CONFIG.ATTN_DROP_RATE,
        residual=True,
    ).to('cuda')

    target_crosser = crosser_module(
        text_embed_dim=768,
        vision_embed_dim=MODEL_CONFIG.EMBED_DIM,
        hidden_dim=MODEL_CONFIG.EMBED_DIM,
        depth=MODEL_CONFIG.CROSS_ATTN_DEPTH,
        num_heads=MODEL_CONFIG.CROSS_NUM_HEADS,
        mlp_ratio=MODEL_CONFIG.MLP_RATIO,
        qkv_bias=True,
        qk_scale=None,
        drop_rate=MODEL_CONFIG.DROP_RATE,
        attn_drop_rate=MODEL_CONFIG.ATTN_DROP_RATE,
        residual=True,
    ).to('cuda')
    
    # model = SiameseTextVisionModel(text_encoder, context_vision_encoder).to('cuda')
    # Optimizer
    optimizer = optim.Adam(
        list(context_vision_encoder.parameters()) +
        list(predictor.parameters()), lr=learning_rate
    )
    
    HIDDEN_RATIO = (0.4, 0.5)
        
    dataset = ImageTextDataset(
        image_path='src/datasets/train', 
        caption_path='src/datasets/annotations/filename_caption_dict.json', 
        batch_size=batch_size,
        img_size=MODEL_CONFIG.SIZE,
        patch_size=MODEL_CONFIG.PATCH_SIZE,
        _hidden_ratio=HIDDEN_RATIO,
        max=max_images_per_epoch,
        transform=transforms.Compose(
            [
                transforms.Resize((MODEL_CONFIG.SIZE, MODEL_CONFIG.SIZE)), 
                transforms.ToTensor()
            ]
        ),
        block_scale=(0.05, 0.1),
        block_aspect_ratio=(0.75, 1.5)
    )
    
    saver = Saver(
        metrics = ['loss'],
        folder_name = 'distributed',
        **asdict(MODEL_CONFIG)
    )

    # ema = (0.999, 1.0)
    ema = (0.996, 1.0)
    ipe_scale = 1.0
    momentum_scheduler = (
        ema[0] + i*(ema[1]-ema[0])/(len(dataset)*num_epochs*ipe_scale)
        for i in range(int(len(dataset)*num_epochs*ipe_scale)+1)
    )
        
    for epoch in range(num_epochs):
        # text_encoder.eval()
        context_vision_encoder.train()
        # target_vision_encoder.eval()
        # target_crosser.eval()
        predictor.train()
        # Initialize tqdm for the dataset
        with tqdm(dataset, desc=f"Epoch {epoch+1}/{num_epochs}") as pbar:
            for images, captions, context_masks, predict_masks in pbar:
                # visualize_rectangle(
                #     context_masks[0].tolist(),
                #     predict_masks[0].tolist(),
                # )
                # print(images)
                # print(captions)
                # Zero the gradients
                optimizer.zero_grad()

                # Forward pass
                encoded_text_ctx, text_attn_mask = text_encoder_context(captions)
                encoded_image_context = context_vision_encoder(images, context_masks)  # Encode the context patches
                predicted = predictor(encoded_image_context, context_masks, predict_masks)  # Generate predictions based on context
                # print(f"{predicted.shape=}")
                T_context, V_context = context_crosser(encoded_text_ctx, encoded_image_context, text_attn_mask)
                
                encoded_text_target, text_attn_mask = text_encoder_target(captions)
                encoded_image_target = target_vision_encoder(images)  # Encode the full tensor
                
                T_target, V_target = target_crosser(encoded_text_target, encoded_image_target, text_attn_mask)
                print(encoded_image_target[0][0][:7].tolist())
                print(predicted[0][0][:7].tolist())
                print()
                print(encoded_image_target[0][1][:7].tolist())
                print(predicted[0][1][:7].tolist())
                print()
                print(encoded_image_target[1][0][:7].tolist())
                print(predicted[1][0][:7].tolist())
                print()
                print(encoded_image_target[1][1][:7].tolist())
                print(predicted[1][1][:7].tolist())
                print()

                T_CLS = T_context[:, 0, :]
                V_CLS = V_context[:, 0, :]
                
                
                target = F.layer_norm(V_target, (V_target.size(-1),))  # Normalize the target
                target = apply_masks(target, predict_masks)  # Apply predict mask
                
                # Calculate loss (L1 loss here)
                p_loss = F.smooth_l1_loss(predicted, target)
                
                # text_embeddings, image_embeddings = model(captions, images, context_masks)

                # Calculate the contrastive loss
                # simamese_loss = siamese_contrastive_loss(T_CLS, V_CLS)
                
                # print(predicted[0][0])
                # print(target[0][0])
                
                loss = p_loss # + c_loss
                saver.update_metric(
                    {
                        'loss':loss.tolist()
                    }
                )
                
                # Backward pass and optimization
                loss.backward()
                optimizer.step()
                optimizer.zero_grad()
                
                # Step 3. momentum update of target encoder
                with torch.no_grad():
                    m = next(momentum_scheduler)
                    print(m)
                    for param_q, param_k in zip(context_vision_encoder.parameters(), target_vision_encoder.parameters()):
                        param_k.data.mul_(m).add_((1.-m) * param_q.detach().data)
                        
                # Update tqdm description with current loss values
                pbar.set_postfix({
                    'MEM': torch.cuda.max_memory_allocated() / 1024.**3,
                    'P Loss': p_loss.item(),
                    # 'Clip Loss': c_loss.item()
                    # 'Siamese Loss': simamese_loss.item(),
                    # 'Hinge Loss': hinge_loss.item()
                    # 'Total Loss': loss.item()
                })
        
        saver.save_epoch()


def main():
    train(
        num_epochs=100, 
        max_images_per_epoch=320, 
        batch_size=32,
        learning_rate=0.001
    )
    
if __name__ == "__main__":
    main()
