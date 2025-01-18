import os
import torch
import numpy as np
from tqdm import tqdm
import torch.nn.functional as F

import comfy.model_management as mm
from comfy.utils import ProgressBar

script_directory = os.path.dirname(os.path.abspath(__file__))

from .noisewarp.noise_warp import NoiseWarper, mix_new_noise
from .noisewarp.raft import RaftOpticalFlow

def get_downtemp_noise(noise, noise_downtemp_interp, interp_to=13):   
    if noise_downtemp_interp == 'nearest':
        return resize_list(noise, interp_to)
    elif noise_downtemp_interp == 'blend':
        return downsamp_mean(noise, interp_to)
    elif noise_downtemp_interp == 'blend_norm':
        return normalized_noises(downsamp_mean(noise, interp_to))
    elif noise_downtemp_interp == 'randn':
        return torch.randn_like(resize_list(noise, interp_to))
    else:
        return noise

def downsamp_mean(x, l=13):
    return torch.stack([sum(u) / len(u) for u in split_into_n_sublists(x, l)])

def normalized_noises(noises):
    #Noises is in TCHW form
    return torch.stack([x / x.std(1, keepdim=True) for x in noises])

def resize_list(array:list, length: int):
    assert isinstance(length, int), "Length must be an integer, but got %s instead"%repr(type(length))
    assert length >= 0, "Length must be a non-negative integer, but got %i instead"%length

    if len(array) > 1 and length > 1:
        step = (len(array) - 1) / (length - 1)
    else:
        step = 0  # default step size to 0 if array has only 1 element or target length is 1
        
    indices = [round(i * step) for i in range(length)]
    
    if isinstance(array, np.ndarray) or isinstance(array, torch.Tensor):
        return array[indices]
    else:
        return [array[i] for i in indices]
    
def split_into_n_sublists(l, n):
    if n <= 0:
        raise ValueError("n must be greater than 0 but n is "+str(n))

    if isinstance(l, str):
        return ''.join(split_into_n_sublists(list(l), n))

    L = len(l)
    indices = [int(i * L / n) for i in range(n + 1)]
    return [l[indices[i]:indices[i + 1]] for i in range(n)]

class GetWarpedNoiseFromVideo:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "Input images to be warped"}),
                "noise_channels": ("INT", {"default": 16, "min": 1, "max": 256, "step": 1}),
                "noise_downtemp_interp": (["nearest", "blend", "blend_norm", "randn", "disabled"], {"tooltip": "Interpolation method(s) for down-temporal noise"}),
                "target_latent_count": ("INT", {"default": 13, "min": 1, "max": 2048, "step": 1, "tooltip": "Interpolate to this many latent frames"}),
                "degradation": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Degradation level(s) for the noise warp"}),
                "latent_shape": (["BTCHW", "BCTHW", "BCHW"], {"tooltip": "Shape of the output latent tensor, for example CogVideoX uses BCTHW, while HunYuanVideo uses BTCHW"}),
                "seed": ("INT", {"default": 123,"min": 0, "max": 0xffffffffffffffff, "step": 1}),
            },
        }
    RETURN_TYPES = ("LATENT", "IMAGE",)
    RETURN_NAMES = ("noise", "visualization",)
    FUNCTION = "warp"
    CATEGORY = "NoiseWarp"

    def warp(self, images, noise_channels, noise_downtemp_interp, degradation, target_latent_count, latent_shape, seed):
        device = mm.get_torch_device()
        torch.manual_seed(seed)
        downscale_factor = 1
        resize_flow = 1
        resize_frames = 1
        downscale_factor=round(resize_frames * resize_flow) * 8
    
        raft_model = RaftOpticalFlow(device, "large")

        # Load video frames into a [T, H, W, C] numpy array, where C=3 and values are between 0 and 1

        #If resize_frames is specified, resize all frames to that (height, width)
        # video_frames = images.numpy()
        # video_frames = video_frames.astype(np.float16)/255
        B, H, W, C = images.shape
        video_frames = images.permute(0, 3, 1, 2)
        
        def downscale_noise(noise):
            down_noise = F.interpolate(noise, scale_factor=1/downscale_factor, mode='area')  # Avg pooling
            down_noise = down_noise * downscale_factor #Adjust for STD
            return down_noise

        warper = NoiseWarper(
            c = noise_channels,
            h = resize_flow * H,
            w = resize_flow * W,
            device = device,
            post_noise_alpha = 0,
            progressive_noise_alpha = 0,
        )

        prev_video_frame = video_frames[0]
        noise = warper.noise

        down_noise = downscale_noise(noise)
        numpy_noise = down_noise.cpu().numpy().astype(np.float16) # In HWC form. Using float16 to save RAM, but it might cause problems on come CPU

        numpy_noises = [numpy_noise]
        numpy_flows = []
        pbar = ProgressBar(len(video_frames) - 1)
        for video_frame in tqdm(video_frames[1:], desc="Calculating noise warp", leave=False):
            dx, dy = raft_model(prev_video_frame, video_frame)
            noise = warper(dx, dy).noise
            prev_video_frame = video_frame

            numpy_flow = np.stack(
                [
                    dx.cpu().numpy().astype(np.float16),
                    dy.cpu().numpy().astype(np.float16),
                ]
            )
            numpy_flows.append(numpy_flow)
            down_noise = downscale_noise(noise)
            numpy_noise = down_noise.cpu().numpy().astype(np.float16)
            numpy_noises.append(numpy_noise)
            pbar.update(1)
        
        numpy_noises = np.stack(numpy_noises).astype(np.float16)
        numpy_flows = np.stack(numpy_flows).astype(np.float16)
        
       
        vis_tensor_noises = torch.from_numpy(numpy_noises)# T, B, C, H, W
        vis_tensor_noises = vis_tensor_noises[:, :, :min(noise_channels, 3), :, :]      
        vis_tensor_noises = vis_tensor_noises.squeeze(1).permute(0, 2, 3, 1).cpu().float()

        noise_tensor = torch.from_numpy(numpy_noises).squeeze(1).cpu().float()

        downtemp_noise_tensor = get_downtemp_noise(
            noise_tensor,
            noise_downtemp_interp=noise_downtemp_interp,
            interp_to=target_latent_count,
        ) # B, F, C, H, W
        downtemp_noise_tensor = downtemp_noise_tensor[None]
        downtemp_noise_tensor = mix_new_noise(downtemp_noise_tensor, degradation)

        if latent_shape == "BTCHW":
            downtemp_noise_tensor = downtemp_noise_tensor.permute(0, 2, 1, 3, 4)
        elif latent_shape == "BCHW":
            downtemp_noise_tensor = downtemp_noise_tensor.squeeze(0)

        return {"samples":downtemp_noise_tensor}, vis_tensor_noises,


NODE_CLASS_MAPPINGS = {
    "GetWarpedNoiseFromVideo": GetWarpedNoiseFromVideo,
    }
NODE_DISPLAY_NAME_MAPPINGS = {
    "GetWarpedNoiseFromVideo": "GetWarpedNoiseFromVideo",
    }
