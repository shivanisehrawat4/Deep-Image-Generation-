# Install necessary packages
!pip install diffusers transformers accelerate xformers==0.0.16rc425

# Import necessary libraries
import torch
from torch import autocast
import numpy as np
from transformers import CLIPTextModel, CLIPTokenizer
from diffusers import AutoencoderKL, LMSDiscreteScheduler, UNet2DConditionModel
from diffusers.schedulers.scheduling_ddim import DDIMScheduler
from tqdm import tqdm
from PIL import Image

class ImageDiffusionModel:
    
    def __init__(self, vae_model, text_tokenizer, text_encoder, unet_model, lms_scheduler, ddim_scheduler):
        self.vae_model = vae_model
        self.text_tokenizer = text_tokenizer
        self.text_encoder = text_encoder
        self.unet_model = unet_model
        self.lms_scheduler = lms_scheduler
        self.ddim_scheduler = ddim_scheduler
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
    
    def get_text_embeddings(self, text):
        # Tokenize the text
        text_input = self.text_tokenizer(text, 
                                    padding='max_length', 
                                    max_length=self.text_tokenizer.model_max_length, 
                                    truncation=True, 
                                    return_tensors='pt')
        # Embed the text
        with torch.no_grad():
            text_embeddings = self.text_encoder(text_input.input_ids.to(self.device))[0]
        return text_embeddings

    def get_prompt_embeddings(self, prompt):
        # Get conditional prompt embeddings
        cond_embeddings = self.get_text_embeddings(prompt)
        # Get unconditional prompt embeddings
        uncond_embeddings = self.get_text_embeddings([''] * len(prompt))
        # Concatenate the above 2 embeddings
        prompt_embeddings = torch.cat([uncond_embeddings, cond_embeddings])
        return prompt_embeddings

    def get_image_latents(self, text_embeddings, image_height=512, image_width=512, 
                          num_inference_steps=50, guidance_scale=7.5, img_latents=None):
        # If no image latent is passed, start reverse diffusion with random noise
        if img_latents is None:
            img_latents = torch.randn((text_embeddings.shape[0] // 2, self.unet_model.in_channels,
                                       image_height // 8, image_width // 8)).to(self.device)
        # Set the number of inference steps for the scheduler
        self.lms_scheduler.set_timesteps(num_inference_steps)
        # Scale the latent embeddings
        img_latents = img_latents * self.lms_scheduler.sigmas[0]
        # Use autocast for automatic mixed precision (AMP) inference
        with autocast('cuda'):
            for i, t in tqdm(enumerate(self.lms_scheduler.timesteps)):
                # Do a single forward pass for both the conditional and unconditional latents
                latent_model_input = torch.cat([img_latents] * 2)
                sigma = self.lms_scheduler.sigmas[i]
                latent_model_input = latent_model_input / ((sigma ** 2 + 1) ** 0.5)
                # Predict noise residuals
                with torch.no_grad():
                    noise_predictions = self.unet_model(latent_model_input, t, encoder_hidden_states=text_embeddings)['sample']
                # Separate predictions for unconditional and conditional outputs
                noise_pred_uncond, noise_pred_cond = noise_predictions.chunk(2)
                # Perform guidance
                noise_predictions = noise_pred_uncond + guidance_scale * (noise_pred_cond - noise_pred_uncond)
                # Remove the noise from the current sample (i.e., go from x_t to x_{t-1})
                img_latents = self.lms_scheduler.step(noise_predictions, t, img_latents)['prev_sample']
        return img_latents

    def decode_image_latents(self, img_latents):
        img_latents = img_latents / 0.18215
        with torch.no_grad():
            images = self.vae_model.decode(img_latents)["sample"]
        # Load images on the CPU
        images = images.detach().cpu()
        return images

    def transform_images(self, images):
        # Transform images from the range [-1, 1] to [0, 1]
        images = (images / 2 + 0.5).clamp(0, 1)
        # Permute the channels and convert to numpy arrays
        images = images.permute(0, 2, 3, 1).numpy()
        # Scale images to the range [0, 255] and convert to int
        images = (images * 255).round().astype('uint8')        
        # Convert to PIL Image objects
        images = [Image.fromarray(img) for img in images]
        return images
    
    def prompts_to_images(self, prompts, image_height=512, image_width=512, 
                          num_inference_steps=50, guidance_scale=7.5, img_latents=None):
        # Convert prompt to a list
        if isinstance(prompts, str):
            prompts = [prompts]
        # Get prompt embeddings
        text_embeddings = self.get_prompt_embeddings(prompts)
        # Get image embeddings
        img_latents = self.get_image_latents(text_embeddings, image_height, image_width,
                                            num_inference_steps, guidance_scale, img_latents)
        # Decode the image embeddings
        images = self.decode_image_latents(img_latents)
        # Convert decoded images to suitable PIL Image format
        images = self.transform_images(images)
        return images

# Load the autoencoder
device = 'cuda'
autoencoder_model = AutoencoderKL.from_pretrained('CompVis/stable-diffusion-v1-4', 
                                            subfolder='vae').to(device)

# Load the tokenizer and the text encoder
text_tokenizer = CLIPTokenizer.from_pretrained('openai/clip-vit-large-patch14')
text_encoder = CLIPTextModel.from_pretrained('openai/clip-vit-large-patch14').to(device)

# Load the UNet model
unet_model = UNet2DConditionModel.from_pretrained('CompVis/stable-diffusion-v1-4', subfolder='unet').to(device)

# Load the schedulers
lms_scheduler = LMSDiscreteScheduler(beta_start=0.00085, 
                                     beta_end=0.012, 
                                     beta_schedule='scaled_linear', 
                                     num_train_timesteps=1000)

ddim_scheduler = DDIMScheduler(beta_start=0.00085, 
                               beta_end=0.012, 
                               beta_schedule='scaled_linear', 
                               num_train_timesteps=1000)

# Create the ImageDiffusionModel instance
image_model = ImageDiffusionModel(autoencoder_model, text_tokenizer, text_encoder, unet_model, lms_scheduler, ddim_scheduler)

# Define prompts for image generation
prompts_list = ["Create a stunning ethereal forest at twilight, mystical, ultra HD, sharp focus, enchanted, luminous, surreal",
                "Design a futuristic cityscape with vibrant neon lights, 8K resolution, sci-fi, cyberpunk, sharp details, holographic",
                "Generate a serene beach sunset with pastel colors, calm, 4K resolution, tranquil, id"]

# Create a function for generating images with new prompts
def generate_images_with_prompts(image_model, prompts):
    generated_images = image_model.prompts_to_images(prompts)
    return generated_images

# Generate images using the function
generated_images = generate_images_with_prompts(image_model, prompts_list)

# Display or save the generated images
for i, img in enumerate(generated_images):
    img.save(f'picture_{i}.jpg')
