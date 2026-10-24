# flm-slotfix — FastFlowLM with the connection-slot leak fixed

A voice agent abandons LLM answers all the time (the user talks over it, or
pauses and then keeps going). On FLM's `/v1/chat/completions`, dropping the
stream is the right move: FLM cancels the generation and frees the NPU in
**< 0.3 ms**. But stock `flm serve` leaks one connection slot per dropped
stream ([ROCm/FastFlowLM#680](https://github.com/ROCm/FastFlowLM/issues/680),
still open in v1.0.10), and at the `-s` limit (we run `-s 4`) it refuses every
connection until restarted — silently pushing this agent *and* the
Personal-Database onto the Ollama fallback.

`build-deb.sh` rebuilds an official release with `connection-slot-release.patch`
inside a container made from FLM's own build `Dockerfile`, and packages it as
`fastflowlm_<version>+slotfix<N>_amd64.deb`.

```bash
tools/flm-slotfix/build-deb.sh v1.0.10          # ~10 min first time (cached after)
systemctl --user stop flm-npu                   # let FLM go idle first
sudo dpkg -i tools/flm-slotfix/dist/fastflowlm_1.0.10+slotfix1_amd64.deb
sudo apt-mark hold fastflowlm                   # don't let a stock package replace it
```

`./stack.sh` starts `flm-npu` again and reports the package. The agent checks the
installed version itself (`FLM_ABORT_ON_CANCEL=auto`, the default): with a
`+slotfix` package it drops abandoned FLM streams; with a stock package it keeps
draining them, because dropping would leak slots. Force either way with
`FLM_ABORT_ON_CANCEL=1` / `0`.

**On every FLM upgrade:** check whether #680 is fixed upstream. If not, run
`build-deb.sh <new tag>` (the patch may need refreshing), then
`sudo apt-mark unhold fastflowlm`, install, and hold again.

Verified 2026-10-24 on v1.0.10, `-s 4`: 8 mid-stream drops → 0 refused
connections (stock: refused after the 3rd); `FlmLLM` suite 6/6.
