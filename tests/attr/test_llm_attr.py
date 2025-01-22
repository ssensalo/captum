#!/usr/bin/env python3

# pyre-strict

import copy

from collections import UserDict
from typing import (
    Any,
    cast,
    Dict,
    List,
    Literal,
    NamedTuple,
    Optional,
    overload,
    Tuple,
    Type,
    Union,
)

import torch
from captum._utils.models.linear_model import SkLearnLasso
from captum._utils.typing import BatchEncodingType
from captum.attr._core.feature_ablation import FeatureAblation
from captum.attr._core.kernel_shap import KernelShap
from captum.attr._core.layer.layer_gradient_shap import LayerGradientShap
from captum.attr._core.layer.layer_gradient_x_activation import LayerGradientXActivation
from captum.attr._core.layer.layer_integrated_gradients import LayerIntegratedGradients
from captum.attr._core.lime import Lime
from captum.attr._core.llm_attr import LLMAttribution, LLMGradientAttribution
from captum.attr._core.shapley_value import ShapleyValues, ShapleyValueSampling
from captum.attr._utils.attribution import GradientAttribution, PerturbationAttribution
from captum.attr._utils.interpretable_input import TextTemplateInput, TextTokenInput
from captum.testing.helpers import BaseTest
from captum.testing.helpers.basic import assertTensorAlmostEqual, rand_like
from parameterized import parameterized, parameterized_class
from torch import nn, Tensor


class DummyTokenizer:
    vocab_size: int = 256
    sos: int = 0
    unk: int = 1
    sos_str: str = "<sos>"
    special_tokens: Dict[int, str] = {sos: sos_str, unk: "<unk>"}

    @overload
    def encode(
        self, text: str, add_special_tokens: bool = ..., return_tensors: None = ...
    ) -> List[int]: ...

    @overload
    def encode(
        self,
        text: str,
        add_special_tokens: bool = ...,
        return_tensors: Literal["pt"] = ...,
    ) -> Tensor: ...

    def encode(
        self,
        text: str,
        add_special_tokens: bool = True,
        return_tensors: Optional[str] = None,
    ) -> Union[List[int], Tensor]:
        tokens = text.split(" ")

        tokens_ids: Union[List[int], Tensor] = [
            ord(s[0]) if len(s) == 1 else (self.sos if s == self.sos_str else self.unk)
            for s in tokens
        ]

        # start with sos
        if add_special_tokens:
            tokens_ids = [self.sos, *tokens_ids]

        if return_tensors:
            return torch.tensor([tokens_ids])
        return tokens_ids

    @overload
    def convert_ids_to_tokens(self, token_ids: List[int]) -> List[str]: ...
    @overload
    def convert_ids_to_tokens(self, token_ids: int) -> str: ...

    def convert_ids_to_tokens(
        self, token_ids: Union[List[int], int]
    ) -> Union[List[str], str]:
        if isinstance(token_ids, int):
            return (
                self.special_tokens[token_ids]
                if token_ids in self.special_tokens
                else chr(token_ids)
            )
        return [
            (self.special_tokens[tid] if tid in self.special_tokens else chr(tid))
            for tid in token_ids
        ]

    @overload
    def convert_tokens_to_ids(self, tokens: str) -> int: ...
    @overload
    def convert_tokens_to_ids(self, tokens: List[str]) -> List[int]: ...

    def convert_tokens_to_ids(
        self, tokens: Union[List[str], str]
    ) -> Union[List[int], int]:
        raise NotImplementedError

    def decode(self, token_ids: Tensor) -> str:
        tokens = self.convert_ids_to_tokens(token_ids.tolist())
        # pyre-fixme[7]: Expected `str` but got `Union[List[str], str]`.
        return tokens if isinstance(tokens, str) else " ".join(tokens)

    def __call__(
        self,
        text: Optional[Union[str, List[str], List[List[str]]]] = None,
        add_special_tokens: bool = True,
        return_offsets_mapping: bool = False,
    ) -> BatchEncodingType:
        assert isinstance(text, str)
        input_ids = self.encode(text, add_special_tokens=add_special_tokens)

        result: BatchEncodingType = UserDict()
        result["input_ids"] = input_ids

        if return_offsets_mapping:
            offset_mapping = []
            if add_special_tokens:
                offset_mapping.append((0, 0))
            idx = 0
            for token in text.split(" "):
                offset_mapping.append((idx - (0 if idx == 0 else 1), idx + len(token)))
                idx += len(token) + 1  # +1 for space
            result["offset_mapping"] = offset_mapping

        return result


class Result(NamedTuple):
    logits: Tensor
    past_key_values: Tensor


class DummyLLM(nn.Module):
    def __init__(self, deterministic_weights: bool = False) -> None:

        super().__init__()
        self.tokenizer = DummyTokenizer()
        self.emb = nn.Embedding(self.tokenizer.vocab_size, 10)
        self.linear = nn.Linear(10, self.tokenizer.vocab_size)
        self.trans = nn.TransformerEncoderLayer(d_model=10, nhead=2, batch_first=True)
        if deterministic_weights:
            self.emb.weight.data = rand_like(self.emb.weight)

            self.trans.eval()

            self_attn_in_weight = self.trans.self_attn.in_proj_weight
            self.trans.self_attn.in_proj_weight.data = rand_like(self_attn_in_weight)
            self.trans.self_attn.in_proj_bias.data.fill_(0.0)

            self_attn_out_weight = self.trans.self_attn.out_proj.weight
            self.trans.self_attn.out_proj.weight.data = rand_like(self_attn_out_weight)
            self.trans.self_attn.out_proj.bias.data.fill_(0.0)

            self.trans.linear1.weight.data = rand_like(self.trans.linear1.weight)
            self.trans.linear1.bias.data.fill_(0.0)

            self.trans.linear2.weight.data = rand_like(self.trans.linear2.weight)
            self.trans.linear2.bias.data.fill_(0.0)

            self.linear.weight.data = rand_like(self.linear.weight)
            self.linear.bias.data.fill_(0.5)

    def forward(self, input_ids: Tensor, *args: Any, **kwargs: Any) -> Result:
        emb = self.emb(input_ids)
        if "past_key_values" in kwargs:
            emb = torch.cat((kwargs["past_key_values"], emb), dim=1)
        encoding = self.trans(emb)
        logits = self.linear(encoding)
        return Result(logits=logits, past_key_values=emb)

    def generate(
        self,
        input_ids: Tensor,
        *args: Any,
        mock_response: Optional[str] = None,
        **kwargs: Any,
    ) -> Tensor:
        assert mock_response, "must mock response to use DummyLLM to generate"
        response = self.tokenizer.encode(mock_response)[1:]
        return torch.cat(
            # pyre-fixme[6]: In call `torch._C._VariableFunctions.cat`,
            # for 1st positional argument, expected `Union[List[Tensor],
            # typing.Tuple[Tensor, ...]]` but got `List[Union[List[int], Tensor]]`.
            [input_ids, torch.tensor([response], device=self.device)],  # type: ignore
            dim=1,
        )

    def _update_model_kwargs_for_generation(
        self, outputs: Result, model_kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        new_kwargs = copy.deepcopy(model_kwargs)
        if hasattr(outputs, "past_key_values"):
            new_kwargs["past_key_values"] = outputs.past_key_values
        return new_kwargs

    def prepare_inputs_for_generation(
        self, model_inp: Tensor, **model_kwargs: Any
    ) -> Dict[str, Tensor]:
        model_inp = model_inp.to(self.device)
        if "past_key_values" in model_kwargs:
            emb_len = model_kwargs["past_key_values"].shape[1]
            return {
                "input_ids": model_inp[:, emb_len:],
                "past_key_values": model_kwargs["past_key_values"],
            }
        if "attention_mask" in model_kwargs:
            return {
                "input_ids": model_inp,
                "attention_mask": model_kwargs["attention_mask"],
            }
        return {"input_ids": model_inp}

    @property
    def device(self) -> torch.device:
        return next(self.parameters()).device


@parameterized_class(
    ("device", "use_cached_outputs"),
    (
        [("cpu", True), ("cpu", False), ("cuda", True), ("cuda", False)]
        if torch.cuda.is_available()
        else [("cpu", True), ("cpu", False)]
    ),
)
# pyre-fixme[13]: Attribute `device` is never initialized.
# pyre-fixme[13]: Attribute `use_cached_outputs` is never initialized.
class TestLLMAttr(BaseTest):
    # pyre-fixme[13]: Attribute `device` is never initialized.
    device: str
    # pyre-fixme[13]: Attribute `use_cached_outputs` is never initialized.
    use_cached_outputs: bool

    # pyre-fixme[56]: Pyre was not able to infer the type of argument `comprehension
    @parameterized.expand(
        [
            (
                AttrClass,
                delta,
                n_samples,
                torch.tensor(true_seq_attr),
                torch.tensor(true_tok_attr),
            )
            for AttrClass, delta, n_samples, true_seq_attr, true_tok_attr in zip(
                (FeatureAblation, ShapleyValueSampling, ShapleyValues),  # AttrClass
                (0.001, 0.001, 0.001),  # delta
                (None, 1000, None),  # n_samples
                (  # true_seq_attr
                    [-0.0007, -0.0031, -0.0126, 0.0102],  # FeatureAblation
                    [0.0021, -0.0047, -0.0193, 0.0302],  # ShapleyValueSampling
                    [0.0021, -0.0047, -0.0193, 0.0302],  # ShapleyValues
                ),
                (  # true_tok_attr
                    [  # FeatureAblation
                        [0.0075, 0.0007, -0.0006, 0.0010],
                        [-0.0062, -0.0073, -0.0079, -0.0003],
                        [-0.0020, -0.0050, -0.0056, -0.0011],
                        [0.0113, 0.0034, 0.0006, 0.0047],
                        [-0.0112, 0.0050, 0.0009, 0.0058],
                    ],
                    [  # ShapleyValueSampling
                        [0.0037, -0.0006, -0.0011, -0.0029],
                        [0.0005, 0.0002, -0.0134, 0.0081],
                        [0.0017, 0.0010, -0.0098, 0.0028],
                        [0.0100, -0.0021, 0.0025, 0.0087],
                        [-0.0138, -0.0031, 0.0025, 0.0134],
                    ],
                    [  # ShapleyValues
                        [0.0037, -0.0006, -0.0011, -0.0029],
                        [0.0005, 0.0002, -0.0134, 0.0081],
                        [0.0017, 0.0010, -0.0098, 0.0028],
                        [0.0100, -0.0021, 0.0025, 0.0087],
                        [-0.0138, -0.0031, 0.0025, 0.0134],
                    ],
                ),
            )
        ]
    )
    def test_llm_attr(
        self,
        AttrClass: Type[PerturbationAttribution],
        delta: float,
        n_samples: Optional[int],
        true_seq_attr: Tensor,
        true_tok_attr: Tensor,
    ) -> None:
        attr_kws: Dict[str, int] = {}
        if n_samples is not None:
            attr_kws["n_samples"] = n_samples

        llm = DummyLLM(deterministic_weights=True)
        llm.to(self.device)
        llm.eval()
        tokenizer = DummyTokenizer()
        llm_attr = LLMAttribution(AttrClass(llm), tokenizer)

        inp = TextTemplateInput("{} b {} {} e {}", ["a", "c", "d", "f"])
        res = llm_attr.attribute(
            inp,
            "m n o p q",
            skip_tokens=[0],
            use_cached_outputs=self.use_cached_outputs,
            # pyre-fixme[6]: In call `LLMAttribution.attribute`,
            # for 4th positional argument, expected
            # `Optional[typing.Callable[..., typing.Any]]` but got `int`.
            **attr_kws,  # type: ignore
        )

        self.assertEqual(res.seq_attr.shape, (4,))
        self.assertEqual(cast(Tensor, res.token_attr).shape, (5, 4))
        self.assertEqual(res.input_tokens, ["a", "c", "d", "f"])
        self.assertEqual(res.output_tokens, ["m", "n", "o", "p", "q"])
        self.assertEqual(res.seq_attr.device.type, self.device)
        self.assertEqual(cast(Tensor, res.token_attr).device.type, self.device)

        assertTensorAlmostEqual(
            self,
            actual=res.seq_attr,
            expected=true_seq_attr,
            delta=delta,
            mode="max",
        )
        assertTensorAlmostEqual(
            self,
            actual=res.token_attr,
            expected=true_tok_attr,
            delta=delta,
            mode="max",
        )

    def test_llm_attr_without_target(self) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        fa = FeatureAblation(llm)
        llm_fa = LLMAttribution(fa, tokenizer)

        inp = TextTemplateInput("{} b {} {} e {}", ["a", "c", "d", "f"])
        res = llm_fa.attribute(
            inp,
            gen_args={"mock_response": "x y z"},
            use_cached_outputs=self.use_cached_outputs,
        )

        self.assertEqual(res.seq_attr.shape, (4,))
        self.assertEqual(cast(Tensor, res.token_attr).shape, (3, 4))
        self.assertEqual(res.input_tokens, ["a", "c", "d", "f"])
        self.assertEqual(res.output_tokens, ["x", "y", "z"])
        self.assertEqual(res.seq_attr.device.type, self.device)
        self.assertEqual(cast(Tensor, res.token_attr).device.type, self.device)

    def test_llm_attr_fa_log_prob(self) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        fa = FeatureAblation(llm)
        llm_fa = LLMAttribution(fa, tokenizer, attr_target="log_prob")

        inp = TextTemplateInput("{} b {} {} e {}", ["a", "c", "d", "f"])
        res = llm_fa.attribute(
            inp,
            "m n o p q",
            skip_tokens=[0],
            use_cached_outputs=self.use_cached_outputs,
        )

        # With FeatureAblation, the seq attr in log_prob
        # equals to the sum of each token attr
        assertTensorAlmostEqual(self, res.seq_attr, cast(Tensor, res.token_attr).sum(0))

    # pyre-fixme[56]: Pyre was not able to infer the type of argument `comprehension
    @parameterized.expand(
        [
            (
                AttrClass,
                delta,
                n_samples,
                torch.tensor(true_seq_attr),
                interpretable_model,
            )
            for AttrClass, delta, n_samples, true_seq_attr, interpretable_model in zip(
                (Lime, KernelShap),
                (0.003, 0.001),
                (1000, 2500),
                (
                    [0.0000, -0.0032, -0.0158, 0.0231],
                    [0.0021, -0.0047, -0.0193, 0.0302],
                ),
                (SkLearnLasso(alpha=0.001), None),
            )
        ]
    )
    def test_llm_attr_without_token(
        self,
        AttrClass: Type[PerturbationAttribution],
        delta: float,
        n_samples: int,
        true_seq_attr: Tensor,
        interpretable_model: Optional[nn.Module] = None,
    ) -> None:
        init_kws = {}
        if interpretable_model is not None:
            init_kws["interpretable_model"] = interpretable_model
        attr_kws: Dict[str, int] = {}
        if n_samples is not None:
            attr_kws["n_samples"] = n_samples

        llm = DummyLLM(deterministic_weights=True)
        llm.to(self.device)
        llm.eval()
        tokenizer = DummyTokenizer()
        fa = AttrClass(llm, **init_kws)
        llm_fa = LLMAttribution(fa, tokenizer, attr_target="log_prob")

        inp = TextTemplateInput("{} b {} {} e {}", ["a", "c", "d", "f"])
        res = llm_fa.attribute(
            inp,
            "m n o p q",
            skip_tokens=[0],
            use_cached_outputs=self.use_cached_outputs,
            **attr_kws,  # type: ignore
        )

        self.assertEqual(res.seq_attr.shape, (4,))
        self.assertEqual(res.seq_attr.device.type, self.device)
        self.assertEqual(res.token_attr, None)
        self.assertEqual(res.input_tokens, ["a", "c", "d", "f"])
        self.assertEqual(res.output_tokens, ["m", "n", "o", "p", "q"])
        assertTensorAlmostEqual(
            self,
            actual=res.seq_attr,
            expected=true_seq_attr,
            delta=delta,
            mode="max",
        )

    def test_futures_not_implemented(self) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        fa = FeatureAblation(llm)
        llm_fa = LLMAttribution(fa, tokenizer)
        attributions = None
        with self.assertRaises(NotImplementedError):
            attributions = llm_fa.attribute_future()
        self.assertEqual(attributions, None)

    def test_llm_attr_with_no_skip_tokens(self) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        fa = FeatureAblation(llm)
        llm_fa = LLMAttribution(fa, tokenizer)

        inp = TextTokenInput("a b c", tokenizer)
        res = llm_fa.attribute(
            inp,
            "m n o p q",
            use_cached_outputs=self.use_cached_outputs,
        )

        # 5 output tokens, 4 input tokens including sos
        self.assertEqual(res.seq_attr.shape, (4,))
        assert res.token_attr is not None
        self.assertIsNotNone(res.token_attr)
        token_attr = res.token_attr
        self.assertEqual(token_attr.shape, (6, 4))
        self.assertEqual(res.input_tokens, ["<sos>", "a", "b", "c"])
        self.assertEqual(res.output_tokens, ["<sos>", "m", "n", "o", "p", "q"])

    def test_llm_attr_with_skip_tensor_target(self) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        fa = FeatureAblation(llm)
        llm_fa = LLMAttribution(fa, tokenizer)

        inp = TextTokenInput("a b c", tokenizer)
        res = llm_fa.attribute(
            inp,
            torch.tensor(tokenizer.encode("m n o p q")),
            skip_tokens=[0],
        )

        # 5 output tokens, 4 input tokens including sos
        self.assertEqual(res.seq_attr.shape, (4,))
        assert res.token_attr is not None
        self.assertIsNotNone(res.token_attr)
        token_attr = res.token_attr
        self.assertEqual(token_attr.shape, (5, 4))
        self.assertEqual(res.input_tokens, ["<sos>", "a", "b", "c"])
        self.assertEqual(res.output_tokens, ["m", "n", "o", "p", "q"])


@parameterized_class(
    ("device",), [("cpu",), ("cuda",)] if torch.cuda.is_available() else [("cpu",)]
)
class TestLLMGradAttr(BaseTest):
    # pyre-fixme[13]: Attribute `device` is never initialized.
    device: str

    @parameterized.expand(
        [
            (LayerIntegratedGradients, None),
            (LayerGradientXActivation, None),
            (LayerGradientShap, (torch.tensor([[1, 0, 1, 0]]),)),
        ]
    )
    def test_llm_attr(
        self, AttrClass: Type[GradientAttribution], baselines: Optional[Tuple[Tensor]]
    ) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        attr = AttrClass(llm, llm.emb)  # type: ignore[call-arg]
        llm_attr = LLMGradientAttribution(attr, tokenizer)

        attr_kws: Dict[str, Any] = {}
        if baselines is not None:
            attr_kws["baselines"] = tuple(
                baseline.to(self.device) for baseline in baselines
            )

        inp = TextTokenInput("a b c", tokenizer)
        res = llm_attr.attribute(inp, "m n o p q", skip_tokens=[0], **attr_kws)

        # 5 output tokens, 4 input tokens including sos
        self.assertEqual(res.seq_attr.shape, (4,))
        assert res.token_attr is not None
        self.assertIsNotNone(res.token_attr)
        token_attr = res.token_attr
        self.assertEqual(token_attr.shape, (5, 4))
        self.assertEqual(res.input_tokens, ["<sos>", "a", "b", "c"])
        self.assertEqual(res.output_tokens, ["m", "n", "o", "p", "q"])

        self.assertEqual(res.seq_attr.device.type, self.device)
        assert res.token_attr is not None
        self.assertEqual(token_attr.device.type, self.device)

    @parameterized.expand(
        [
            (LayerIntegratedGradients, None),
            (LayerGradientXActivation, None),
            (LayerGradientShap, (torch.tensor([[1, 0, 1, 0]]),)),
        ]
    )
    def test_llm_attr_without_target(
        self, AttrClass: Type[GradientAttribution], baselines: Optional[Tuple[Tensor]]
    ) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        attr = AttrClass(llm, llm.emb)  # type: ignore[call-arg]
        llm_attr = LLMGradientAttribution(attr, tokenizer)

        attr_kws: Dict[str, Any] = {}
        if baselines is not None:
            attr_kws["baselines"] = tuple(
                baseline.to(self.device) for baseline in baselines
            )

        inp = TextTokenInput("a b c", tokenizer)
        res = llm_attr.attribute(inp, gen_args={"mock_response": "x y z"}, **attr_kws)

        self.assertEqual(res.seq_attr.shape, (4,))
        assert res.token_attr is not None
        self.assertIsNotNone(res.token_attr)
        token_attr = res.token_attr
        self.assertEqual(token_attr.shape, (3, 4))
        self.assertEqual(res.input_tokens, ["<sos>", "a", "b", "c"])
        self.assertEqual(res.output_tokens, ["x", "y", "z"])

        self.assertEqual(res.seq_attr.device.type, self.device)
        assert res.token_attr is not None
        self.assertEqual(token_attr.device.type, self.device)

    @parameterized.expand(
        [
            (LayerIntegratedGradients, None),
            (LayerGradientXActivation, None),
            (LayerGradientShap, (torch.tensor([[1, 0, 1]]),)),
        ]
    )
    def test_llm_attr_with_skip_tokens(
        self, AttrClass: Type[GradientAttribution], baselines: Optional[Tuple[Tensor]]
    ) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        attr = AttrClass(llm, llm.emb)  # type: ignore[call-arg]
        llm_attr = LLMGradientAttribution(attr, tokenizer)

        attr_kws: Dict[str, Any] = {}
        if baselines is not None:
            attr_kws["baselines"] = tuple(
                baseline.to(self.device) for baseline in baselines
            )

        inp = TextTokenInput("a b c", tokenizer, skip_tokens=[0])
        res = llm_attr.attribute(inp, "m n o p q", skip_tokens=[0], **attr_kws)

        # 5 output tokens, 4 input tokens including sos
        self.assertEqual(res.seq_attr.shape, (3,))
        assert res.token_attr is not None
        self.assertIsNotNone(res.token_attr)
        token_attr = res.token_attr
        self.assertEqual(token_attr.shape, (5, 3))
        self.assertEqual(res.input_tokens, ["a", "b", "c"])
        self.assertEqual(res.output_tokens, ["m", "n", "o", "p", "q"])

        self.assertEqual(res.seq_attr.device.type, self.device)
        assert res.token_attr is not None
        self.assertEqual(token_attr.device.type, self.device)

    def test_llm_attr_with_no_skip_tokens(self) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        attr = LayerIntegratedGradients(llm, llm.emb)  # type: ignore[call-arg]
        llm_attr = LLMGradientAttribution(attr, tokenizer)

        attr_kws: Dict[str, Any] = {}
        inp = TextTokenInput("a b c", tokenizer)
        res = llm_attr.attribute(inp, "m n o p q", **attr_kws)

        # 6 output tokens, 4 input tokens including sos
        self.assertEqual(res.seq_attr.shape, (4,))
        assert res.token_attr is not None
        self.assertIsNotNone(res.token_attr)
        token_attr = res.token_attr
        self.assertEqual(token_attr.shape, (6, 4))
        self.assertEqual(res.input_tokens, ["<sos>", "a", "b", "c"])
        self.assertEqual(res.output_tokens, ["<sos>", "m", "n", "o", "p", "q"])

    def test_llm_attr_with_skip_tensor_target(self) -> None:
        llm = DummyLLM()
        llm.to(self.device)
        tokenizer = DummyTokenizer()
        attr = LayerIntegratedGradients(llm, llm.emb)  # type: ignore[call-arg]
        llm_attr = LLMGradientAttribution(attr, tokenizer)

        attr_kws: Dict[str, Any] = {}
        inp = TextTokenInput("a b c", tokenizer)
        res = llm_attr.attribute(
            inp,
            torch.tensor(tokenizer.encode("m n o p q")),
            skip_tokens=[0],
            **attr_kws,
        )

        # 5 output tokens, 4 input tokens including sos
        self.assertEqual(res.seq_attr.shape, (4,))
        assert res.token_attr is not None
        self.assertIsNotNone(res.token_attr)
        token_attr = res.token_attr
        self.assertEqual(token_attr.shape, (5, 4))
        self.assertEqual(res.input_tokens, ["<sos>", "a", "b", "c"])
        self.assertEqual(res.output_tokens, ["m", "n", "o", "p", "q"])
