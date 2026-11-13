On a host without a bridge network, the overlay now also sets `build.network: host` when `tasks.images: build`, so BuildKit's `RUN` steps no longer fail with `network bridge not found`.
