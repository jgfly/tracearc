import json

from nanovllmlite import LLM, SamplingParams


def setup():
    """Build a tiny config; uses an external (stdlib) call."""
    return json.dumps({"seed": 42, "model": "dummy"})


def factorial(n):
    """Recursion demo (nested, not a loop)."""
    if n <= 1:
        return 1
    return n * factorial(n - 1)


def unused_helper():
    """Never called at runtime -> should be dimmed as an uncalled block."""
    return "i am never called"


def main():
    cfg = setup()                      # external: json.dumps
    llm = LLM(model=cfg, enforce_eager=True)   # constructor chain
    sp = SamplingParams(temperature=0.6, max_tokens=4)
    prompts = ["hello", "count to three"]
    outputs = llm.generate(prompts, sp)       # decode loop
    for prompt, output in zip(prompts, outputs):
        print(prompt, "->", output)
    print("factorial(5) =", factorial(5))     # recursion


if __name__ == "__main__":
    main()
