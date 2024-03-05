import os
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Text, Union, cast

import torch
from openai.types import (
    Completion,
    CompletionChoice,
    CompletionUsage,
    CreateEmbeddingResponse,
)
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice as ChatChoice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from transformers import (
    AutoModel,
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    PreTrainedModel,
    PreTrainedTokenizer,
    PreTrainedTokenizerFast,
    StoppingCriteria,
    StoppingCriteriaList,
)
from transformers.tokenization_utils_base import PreTokenizedInput

from languru.action.base import ActionBase, ModelDeploy
from languru.config import logger
from languru.llm.config import settings as llm_settings
from languru.utils.calculation import mean_pooling, tensor_to_np
from languru.utils.common import must_list, replace_right, should_str_or_none
from languru.utils.device import validate_device, validate_dtype
from languru.utils.hf import StopAtWordsStoppingCriteria, remove_special_tokens

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

# Device config
DEVICE = llm_settings.device = validate_device(device=llm_settings.device)
DTYPE = validate_dtype(
    device=DEVICE,
    dtype=llm_settings.dtype or ("float16" if DEVICE in ("cuda", "mps") else "float32"),
)
logger.info(f"Using device: {DEVICE} with dtype: {DTYPE}")
torch.set_default_device(DEVICE)


class TransformersAction(ActionBase):
    # Model configuration
    MODEL_NAME: Text = (os.getenv("HF_MODEL_NAME") or os.getenv("MODEL_NAME")) or ""
    model_deploys = (
        ModelDeploy(MODEL_NAME, MODEL_NAME),
        ModelDeploy(MODEL_NAME.split("/")[-1], MODEL_NAME),
    )

    # Model Quantization configuration
    use_quantization: bool = bool(
        os.getenv("HF_USE_QUANTIZATION") or os.getenv("USE_QUANTIZATION") or False
    )
    load_in_8bit: Optional[bool] = None
    load_in_4bit: Optional[bool] = None
    llm_int8_threshold: Optional[float] = None
    llm_int8_skip_modules: Optional[Sequence[Text]] = None
    llm_int8_enable_fp32_cpu_offload: Optional[bool] = None
    llm_int8_has_fp16_weight: Optional[bool] = None
    bnb_4bit_compute_dtype: Optional[Text] = None
    bnb_4bit_quant_type: Optional[Text] = None
    bnb_4bit_use_double_quant: Optional[bool] = None

    # Generation configuration
    stop_words: Sequence[Text] = ()
    is_causal_lm: bool = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.dtype = DTYPE
        self.device = DEVICE

        # Model name
        self.model_name = self.read_model_name(**kwargs)
        # Model and tokenizer
        if self.is_causal_lm is True:
            self.model: "PreTrainedModel" = AutoModelForCausalLM.from_pretrained(
                self.model_name,
                device_map=self.device,
                torch_dtype=self.dtype,
                trust_remote_code=True,
                quantization_config=self.load_quantization_config(**kwargs),
            )
        else:
            self.model: "PreTrainedModel" = AutoModel.from_pretrained(
                self.model_name,
                device_map=self.device,
                torch_dtype=self.dtype,
                trust_remote_code=True,
            )
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_name, trust_remote_code=True
        )

    def name(self):
        return "transformers_action"

    def health(self) -> bool:
        param_count = sum(p.numel() for p in self.model.parameters())
        if param_count <= 0:
            return False
        return True

    def chat(
        self, messages: List["ChatCompletionMessageParam"], *args, model: Text, **kwargs
    ) -> "ChatCompletion":
        if len(messages) == 0:
            raise ValueError("The `messages` cannot be empty")

        # Prepare prompt
        stop = kwargs.pop("stop", [])
        if self.tokenizer.chat_template is None:
            prompt = ""
            for m in messages:
                if "content" in m and m["content"] is not None:
                    prompt += f"\n\n{m['role']}:\n{m['content']}"
                prompt = prompt.strip()
            prompt = prompt.strip()
            if len(prompt) == 0:
                raise ValueError("The `prompt` cannot be empty, no content in messages")
            prompt += "\n\nassistant:\n"

            # Prepare stop words
            roles = set(
                [message["role"] for message in messages]
                + ["assistant", "bot", "user", "system"]
            )
            if isinstance(stop, Text):
                stop = [stop]
            elif isinstance(stop, Sequence):
                stop = [str(w) for w in list(stop)]
            else:
                logger.warning(f"Invalid stop words parameters: {stop}")
                stop = []
            stop = [s for s in stop if s]
            stop.extend([f"\n{role}:" for role in roles])

        else:
            tokenizer_chat_conversation = [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if "content" in m and m["content"] is not None
            ]
            prompt = self.tokenizer.apply_chat_template(
                tokenizer_chat_conversation, tokenize=False, add_generation_prompt=True
            )
            assert isinstance(prompt, Text)
            if not prompt:
                raise ValueError("The `prompt` cannot be empty, no content in messages")

        # Chat completion request
        completion_res = self.text_completion(
            prompt=prompt, model=model, stop=stop, **kwargs
        )
        # Parse completion response to chat completion
        chat_completion = ChatCompletion(
            id=completion_res.id,
            choices=[],
            created=completion_res.created,
            model=completion_res.model,
            object="chat.completion",
            system_fingerprint=completion_res.system_fingerprint,
            usage=completion_res.usage,
        )
        # Modify completion response to chat completion
        for c in completion_res.choices:
            message_text = c.text
            for stop_word in stop:
                modified_message_text = replace_right(
                    message_text, old=stop_word, new="", occurrence=1
                )
                if modified_message_text != message_text:
                    message_text = modified_message_text
                    break
            chat_completion.choices.append(
                ChatChoice(
                    finish_reason=c.finish_reason,
                    index=c.index,
                    message=ChatCompletionMessage(
                        role="assistant",
                        content=message_text,
                    ),
                )
            )
        return chat_completion

    def text_completion(
        self, prompt: Text, *args, model: Text, **kwargs
    ) -> "Completion":
        # Validate parameters
        if not prompt:
            raise ValueError("The `prompt` cannot be empty")
        if model != self.model_name:
            logger.warning(
                f"The model `{model}` is not the same as the action's model "
                + f"`{self.model_name}`"
            )

        # Initialize completion response
        completion_res = Completion(
            id=str(uuid.uuid4()),
            choices=[],
            created=int(time.time()),
            model=self.model_name,
            object="text_completion",
        )

        # Prepare kwargs
        max_length = kwargs.get("max_tokens") or kwargs.get("max_length") or None
        if max_length is not None:
            kwargs["max_length"] = int(max_length)
        else:
            kwargs["max_length"] = max_length = self.default_max_tokens
        kwargs.pop("max_tokens", None)
        kwargs.pop("best_of", None)  # TODO: Implement best_of
        kwargs.pop("echo", None)  # TODO: Implement echo
        kwargs.pop("n", None)  # TODO: Implement n
        kwargs.pop("frequency_penalty", None)  # TODO: Implement frequency_penalty
        kwargs.pop("presence_penalty", None)  # TODO: Implement presence_penalty
        kwargs.pop("stream", None)  # TODO: Implement stream
        kwargs.pop("logprobs", None)  # TODO: Implement logprobs
        total_stop_words = must_list(self.stop_words) + must_list(
            kwargs.pop("stop", ())
        )

        # Stopping criteria
        stop_criteria: Optional["StoppingCriteria"] = None
        if len(total_stop_words) > 0:
            stop_criteria = StopAtWordsStoppingCriteria.from_stop_words(
                total_stop_words, self.tokenizer
            )
            kwargs["stopping_criteria"] = StoppingCriteriaList([stop_criteria])

        # Tokenize prompt
        inputs = self.tokenizer(
            prompt, return_tensors="pt", return_attention_mask=False
        )
        input_ids = inputs["input_ids"]
        input_ids = cast(torch.Tensor, input_ids)
        input_ids = input_ids.to(self.device)
        inputs_tokens_length = int(input_ids.shape[1])
        if max_length is not None and inputs_tokens_length >= max_length:
            logger.warning(
                "The input tokens length is already greater than max_length, "
                + f"{inputs_tokens_length} >= {max_length}, "
                + f"resetting max_length to {inputs_tokens_length + 20}"
            )
            kwargs["max_length"] = max_length = inputs_tokens_length + 20

        # Generate text completion
        outputs = self.model.generate(input_ids, **kwargs)
        outputs = cast(torch.Tensor, outputs)
        outputs_tokens_length = outputs.shape[1]
        completed_text = self.tokenizer.batch_decode(outputs)[0]
        output_text = self.remove_echo_text(
            prompt=prompt, completed_text=completed_text, tokenizer=self.tokenizer
        )

        # Collect completion response
        finish_reason = "length"
        if stop_criteria is not None:
            finish_reason = stop_criteria.get_stop_reason() or finish_reason
        elif (
            self.tokenizer.eos_token_id is not None
            and outputs[-1][-1].item() == self.tokenizer.eos_token_id
        ):
            finish_reason = "stop"
        completion_choice = CompletionChoice(
            finish_reason=finish_reason, index=0, text=output_text
        )
        completion_res.choices.append(completion_choice)
        completion_res.usage = CompletionUsage(
            total_tokens=outputs_tokens_length,
            prompt_tokens=inputs_tokens_length,
            completion_tokens=outputs_tokens_length - inputs_tokens_length,
        )
        completion_res.created = int(time.time())
        return completion_res

    def embeddings(
        self,
        input: Union[Text, PreTokenizedInput, List[PreTokenizedInput]],
        *args,
        model: Text,
        **kwargs,
    ) -> "CreateEmbeddingResponse":
        # Validate parameters
        kwargs.pop("encoding_format", None)  # TODO: Implement encoding_format

        # Tokenize prompt
        inputs = self.tokenizer(
            input, return_tensors="pt", padding=True, truncation=True
        )
        inputs = inputs.to(self.device)
        input_ids = self.ensure_tensor(inputs["input_ids"])
        inputs_tokens_length = int(input_ids.shape[1])

        with torch.no_grad():  # No need to compute gradients
            output = self.model(**inputs, **kwargs)
            hidden_states = output.last_hidden_state

        # Perform pooling.
        embeddings = mean_pooling(
            tensor_to_np(tensor=self.ensure_tensor(hidden_states)),
            tensor_to_np(self.ensure_tensor(inputs["attention_mask"])),
        )
        print(embeddings.shape)  # (1, 384)
        return CreateEmbeddingResponse.model_validate(
            {
                "data": [
                    {
                        "embedding": emb,
                        "index": idx,
                        "object": "embedding",
                    }
                    for idx, emb in enumerate(embeddings.tolist())
                ],
                "model": self.model_name,
                "object": "list",
                "usage": {
                    "total_tokens": inputs_tokens_length,
                    "prompt_tokens": inputs_tokens_length,
                },
            }
        )

    def ensure_tensor(self, input: Any) -> "torch.Tensor":
        if isinstance(input, torch.Tensor) is False:
            logger.warning(
                f"Input is not a tensor, converting to tensor: {type(input)}"
            )
        return input

    def read_model_name(self, **kwargs) -> Text:
        model_name = should_str_or_none(kwargs.get("model_name")) or self.MODEL_NAME
        if not model_name:
            raise ValueError("The `model_name` cannot be empty")
        logger.info(f"Using model: {model_name}")
        return model_name

    def load_quantization_config(self, **kwargs) -> Optional["BitsAndBytesConfig"]:
        use_quantization = bool(kwargs.get("use_quantization") or self.use_quantization)
        if use_quantization is True:
            params = {}
            load_in_8bit = (
                kwargs["load_in_8bit"]
                if "load_in_8bit" in kwargs
                else self.load_in_8bit
            )
            load_in_4bit = (
                kwargs["load_in_4bit"]
                if "load_in_4bit" in kwargs
                else self.load_in_4bit
            )
            llm_int8_threshold = (
                kwargs["llm_int8_threshold"]
                if "llm_int8_threshold" in kwargs
                else self.llm_int8_threshold
            )
            llm_int8_skip_modules = (
                kwargs["llm_int8_skip_modules"]
                if "llm_int8_skip_modules" in kwargs
                else self.llm_int8_skip_modules
            )
            llm_int8_enable_fp32_cpu_offload = (
                kwargs["llm_int8_enable_fp32_cpu_offload"]
                if "llm_int8_enable_fp32_cpu_offload" in kwargs
                else self.llm_int8_enable_fp32_cpu_offload
            )
            llm_int8_has_fp16_weight = (
                kwargs["llm_int8_has_fp16_weight"]
                if "llm_int8_has_fp16_weight" in kwargs
                else self.llm_int8_has_fp16_weight
            )
            bnb_4bit_compute_dtype = (
                kwargs["bnb_4bit_compute_dtype"]
                if "bnb_4bit_compute_dtype" in kwargs
                else self.bnb_4bit_compute_dtype
            )
            bnb_4bit_quant_type = (
                kwargs["bnb_4bit_quant_type"]
                if "bnb_4bit_quant_type" in kwargs
                else self.bnb_4bit_quant_type
            )
            bnb_4bit_use_double_quant = (
                kwargs["bnb_4bit_use_double_quant"]
                if "bnb_4bit_use_double_quant" in kwargs
                else self.bnb_4bit_use_double_quant
            )
            if load_in_8bit is not None:
                params["load_in_8bit"] = load_in_8bit
            if load_in_4bit is not None:
                params["load_in_4bit"] = load_in_4bit
            if llm_int8_threshold is not None:
                params["llm_int8_threshold"] = llm_int8_threshold
            if llm_int8_skip_modules is not None:
                params["llm_int8_skip_modules"] = llm_int8_skip_modules
            if llm_int8_enable_fp32_cpu_offload is not None:
                params["llm_int8_enable_fp32_cpu_offload"] = (
                    llm_int8_enable_fp32_cpu_offload
                )
            if llm_int8_has_fp16_weight is not None:
                params["llm_int8_has_fp16_weight"] = llm_int8_has_fp16_weight
            if bnb_4bit_compute_dtype is not None:
                params["bnb_4bit_compute_dtype"] = bnb_4bit_compute_dtype
            if bnb_4bit_quant_type is not None:
                params["bnb_4bit_quant_type"] = bnb_4bit_quant_type
            if bnb_4bit_use_double_quant is not None:
                params["bnb_4bit_use_double_quant"] = bnb_4bit_use_double_quant
            logger.info(f"Using quantization config: {params}")
            return BitsAndBytesConfig(**params)
        else:
            return None

    def remove_echo_text(
        self,
        prompt: Text,
        completed_text: Text,
        tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast],
    ) -> Text:
        """Remove the echo text from the completed text.

        Parameters
        ----------
        prompt : Text
            The prompt text.
        completed_text : Text
            The completed text.
        tokenizer : Union[PreTrainedTokenizer, PreTrainedTokenizerFast]
            The tokenizer used to tokenize the prompt and completed text.

        Returns
        -------
        Text
            The output text without the echo text.
        """

        if tokenizer.eos_token:
            _pat = rf"\s*{re.escape(tokenizer.eos_token)}\s*"
            prompt = re.sub(_pat, "", prompt)
            completed_text = re.sub(_pat, "", completed_text)
        if tokenizer.bos_token:
            _pat = rf"\s*{re.escape(tokenizer.bos_token)}\s*"
            prompt = re.sub(_pat, "", prompt)
            completed_text = re.sub(_pat, "", completed_text)
        output_text = completed_text.replace(prompt, "", 1)
        output_text = remove_special_tokens(output_text, tokenizer=self.tokenizer)
        return output_text
