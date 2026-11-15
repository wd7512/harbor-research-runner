Non-streaming proxy responses now wait for the complete upstream body before choosing the harness status. Stalls and transport failures return retryable errors whose status matches the proxy ledger.
