import crypto from 'node:crypto'

if (typeof crypto.getRandomValues !== 'function') {
  Object.defineProperty(crypto, 'getRandomValues', {
    value: crypto.webcrypto.getRandomValues.bind(crypto.webcrypto),
  })
}

await import('../node_modules/vite/bin/vite.js')
