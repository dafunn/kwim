# kwim-console

The KWIM admin console: a static single-page app (TypeScript, React, Vite) over
the service's `/v1/admin` API. It adds no backend logic of its own. In a
deployment, nginx serves the built assets and the ingress routes `/v1` and
`/health` to kwim-service on the same origin.

See [ARCHITECTURE.md](../../docs/ARCHITECTURE.md) for where the console sits,
[deployment.md](../../docs/deployment.md) for building and deploying it, and
[operations.md](../../docs/operations.md) for creating the first operator account.

## Development

Run from this directory:

```bash
npm ci              # install from the lockfile
npm run dev         # Vite dev server
npm test            # component and integration tests against a mocked API
npm run lint        # oxlint
npm run build       # type-check, build to dist/, then check the bundle
```

The app calls the API on its own origin, so `npm run dev` needs a kwim-service
reachable at the same origin to do anything past the login screen. The tests do
not: they mock the API.

`npm run build` ends with `scripts/check-no-storage.mjs`, which fails the build if
the bundle references `localStorage` or `sessionStorage`. No secret value is kept
in browser storage.

## API types

`src/api/schema.d.ts` is generated from the service's OpenAPI document and
committed. After changing the `/v1/admin` API, regenerate it:

```bash
npm run generate:api
```

This needs the `services/api` virtualenv but no running backends.

## Container image

```bash
docker build -f services/console/Dockerfile -t <registry>/kwim-console services/console
```

The build context is this directory. The runtime image is nginx with the built
assets and `nginx.conf`; it carries no Node runtime.
