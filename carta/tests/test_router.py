class TestLlavaPrompt:
    def test_transcribes_not_infers(self):
        from carta.vision.router import LLAVA_PROMPT
        p = LLAVA_PROMPT.lower()
        assert "transcribe" in p
        assert "not infer" in p                              # forbids inference explicitly
        assert "describe this technical diagram" not in p    # old interpretive opener is gone

    def test_constrains_output_format(self):
        # "Output only ... one per line" keeps model preamble out of the index.
        from carta.vision.router import LLAVA_PROMPT
        p = LLAVA_PROMPT.lower()
        assert "output only" in p
        assert "one per line" in p
