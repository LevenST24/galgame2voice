import { defineConfig } from 'vite';

export default defineConfig({
  base: '/static/',
  build: { outDir: 'dist' },
  server: {
    port: 3015,
    host: '0.0.0.0',
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/audio': 'http://127.0.0.1:8080',
      '/ai': 'http://127.0.0.1:8080',
    },
  },
});
