// Starts PostgREST, the Auth server, and a gateway on $DATABASE_URL, as
// `supabase start` does, then runs the command in the arguments with
// SUPABASE_URL, SUPABASE_ANON_KEY, and SUPABASE_SERVICE_ROLE_KEY set.
import { execFileSync, spawn } from 'node:child_process'
import { createHmac, randomBytes } from 'node:crypto'
import http from 'node:http'
import net from 'node:net'

const POSTGREST = 'postgrest/postgrest:v16.4'
const AUTH = 'supabase/gotrue:v2.197.0'
const JWT_SECRET = randomBytes(32).toString('hex')

function urlAs(role) {
  const url = new URL(process.env.DATABASE_URL)
  url.username = role
  url.searchParams.set('sslmode', 'disable')
  return url.toString()
}

function sign(payload) {
  const part = (o) => Buffer.from(JSON.stringify(o)).toString('base64url')
  const body = `${part({ alg: 'HS256', typ: 'JWT' })}.${part(payload)}`
  return `${body}.${createHmac('sha256', JWT_SECRET).update(body).digest('base64url')}`
}

function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer().listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

const containers = []
function stop() {
  for (const name of containers.splice(0)) execFileSync('docker', ['rm', '-f', name], { stdio: 'ignore' })
}
// Node skips `finally` when a signal ends it, so stop the containers here.
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    stop()
    process.exit(1)
  })
}

function start(image, env, args = []) {
  const name = `migcheck-stack-${randomBytes(4).toString('hex')}`
  const flags = Object.entries(env).flatMap(([k, v]) => ['-e', `${k}=${v}`])
  execFileSync('docker', ['run', '-d', '--rm', '--name', name, '--network', 'host', ...flags, image, ...args], { stdio: 'ignore' })
  containers.push(name)
}

async function waitFor(url) {
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(url)).ok) return
    } catch {}
    await new Promise((r) => setTimeout(r, 200))
  }
  throw new Error(`${url} didn't respond in 20 seconds`)
}

const [restPort, authPort, gatewayPort] = await Promise.all([freePort(), freePort(), freePort()])
const gatewayUrl = `http://127.0.0.1:${gatewayPort}`
let exitCode = 1
try {
  start(POSTGREST, {
    PGRST_DB_URI: urlAs('authenticator'),
    PGRST_DB_SCHEMAS: 'public,graphql_public',
    PGRST_DB_ANON_ROLE: 'anon',
    PGRST_JWT_SECRET: JWT_SECRET,
    PGRST_SERVER_PORT: restPort,
  })
  start(AUTH, {
    GOTRUE_API_HOST: '127.0.0.1',
    GOTRUE_API_PORT: authPort,
    API_EXTERNAL_URL: `${gatewayUrl}/auth/v1`,
    GOTRUE_SITE_URL: gatewayUrl,
    GOTRUE_DB_DRIVER: 'postgres',
    GOTRUE_DB_DATABASE_URL: urlAs('supabase_auth_admin'),
    GOTRUE_JWT_SECRET: JWT_SECRET,
    GOTRUE_JWT_AUD: 'authenticated',
    GOTRUE_JWT_DEFAULT_GROUP_NAME: 'authenticated',
    GOTRUE_JWT_ADMIN_ROLES: 'service_role',
    GOTRUE_JWT_EXP: 3600,
    GOTRUE_EXTERNAL_EMAIL_ENABLED: true,
    GOTRUE_MAILER_AUTOCONFIRM: true,
  })

  // Routes /rest/v1 and /auth/v1 as Supabase's API gateway does.
  const routes = { '/rest/v1': restPort, '/auth/v1': authPort }
  const gateway = http.createServer((req, res) => {
    const prefix = Object.keys(routes).find((p) => req.url.startsWith(p))
    if (!prefix) return res.writeHead(404).end()
    const upstream = http.request(
      { host: '127.0.0.1', port: routes[prefix], path: req.url.slice(prefix.length) || '/', method: req.method, headers: req.headers },
      (up) => {
        res.writeHead(up.statusCode, up.headers)
        up.pipe(res)
      },
    )
    upstream.on('error', () => res.writeHead(502).end())
    req.pipe(upstream)
  })
  await new Promise((resolve) => gateway.listen(gatewayPort, '127.0.0.1', resolve))
  gateway.unref()

  await Promise.all([waitFor(`http://127.0.0.1:${restPort}/`), waitFor(`http://127.0.0.1:${authPort}/health`)])

  const [command, ...args] = process.argv.slice(2)
  const child = spawn(command, args, {
    stdio: 'inherit',
    env: {
      ...process.env,
      SUPABASE_URL: gatewayUrl,
      SUPABASE_ANON_KEY: sign({ role: 'anon', iss: 'supabase' }),
      SUPABASE_SERVICE_ROLE_KEY: sign({ role: 'service_role', iss: 'supabase' }),
    },
  })
  exitCode = await new Promise((resolve, reject) => {
    child.on('error', reject)
    child.on('exit', (code) => resolve(code ?? 1))
  })
} finally {
  stop()
}
process.exit(exitCode)
