# How to turn an incident into a test

1. **Describe the observed shape in one sentence.** Say what appeared on disk or on the wire, not what you think the code did.
2. **Choose the lowest tier that can reproduce it.** A wrong classification belongs in a unit test. An upstream response belongs in an integration test. Harbor resume behaviour, Docker, or a killed process belongs in an end-to-end test.
3. **Add the incident to `incidents.md`.** Record its shape, tier, test name, and step so the incident list remains complete.
4. **Write the test before the guard.** Make it fail when the protection is absent, run it, and include the observed failure in the change report.

Keep the test focused on output: verdicts, ledger rows, files, folders, or command exit codes. Follow the boundaries and timing rules in [How to write a test](how-to-write-a-test.md).
