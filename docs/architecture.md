# Portable Runtime architecture

`portable_runtime` keeps durable state in the Runtime and treats every model,
harness, tool, verifier, trigger and human channel as a replaceable provider.

```text
Work / Run / Artifact / Evidence / Knowledge
                    |
              Runtime + Store
                    |
          CapabilityService + Router
                    |
             ProviderRegistry
                    |
      in-process or stdio-jsonl providers
```

The core imports no concrete provider or deployment. The legacy
`control_plane` package remains under `compat` by behavior, not by import
alias, until each workflow is migrated with parity tests.
