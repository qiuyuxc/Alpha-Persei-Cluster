import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'node:path'

// 独立发布子 GM 的三个文件，不覆盖主后台产物。
export default defineConfig({
  base: './',
  plugins: [tailwindcss(), react()],
  build: {
    outDir: resolve(__dirname, '../static_web'),
    emptyOutDir: false,
    cssCodeSplit: false,
    rollupOptions: {
      input: resolve(__dirname, 'sub_gm.html'),
      output: {
        inlineDynamicImports: true,
        entryFileNames: 'sub-gm.js',
        assetFileNames: 'sub-gm[extname]',
      },
    },
  },
})
