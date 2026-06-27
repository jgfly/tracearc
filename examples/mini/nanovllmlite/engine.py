import hashlib


class LLMEngine:
    """Mimics nano-vllm's LLMEngine orchestration."""

    def __init__(self, model, enforce_eager=False):
        self.model_name = model
        self.eager = enforce_eager
        self.model = QwenModel()                       # constructor chain
        # external (stdlib) call at the boundary
        self._id = hashlib.md5(model.encode()).hexdigest()

    def generate(self, prompts, sampling_params):
        """The main decode loop — collapses to a single ↻×N body."""
        outputs = []
        while not self._is_finished(len(outputs), len(prompts),
                                     sampling_params.max_tokens):
            self.step(prompts, sampling_params, outputs)
        return outputs

    def _is_finished(self, n_out, n_prompts, max_tokens):
        return n_out >= n_prompts * max_tokens

    def step(self, prompts, sampling_params, outputs):
        logits = self.model.forward(prompts, len(outputs))
        token = self._sample(logits, sampling_params)
        outputs.append(token)

    def _sample(self, logits, sampling_params):
        # external-ish: just an int op here
        return int(logits) % 256


class LLM(LLMEngine):
    """Thin subclass — exercises inheritance edges (off by default)."""
    pass


class QwenModel:
    """Mimics the model forward path."""

    def __init__(self):
        self.layers = [Linear() for _ in range(3)]     # loop over layers

    def forward(self, prompts, step):
        x = step + len(prompts)
        for layer in self.layers:                      # collapses to ↻×3
            x = layer.forward(x)
        return x


class Linear:
    def __init__(self):
        self.weight = 7

    def forward(self, x):
        return (x * self.weight + 1) % 1024
