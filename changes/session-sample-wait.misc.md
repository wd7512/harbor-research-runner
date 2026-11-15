The session integration test waits up to 4 s, not 2 s, for the sampler's first sample, which needs a full Docker measurement and timed out on a busy Ubuntu runner.
