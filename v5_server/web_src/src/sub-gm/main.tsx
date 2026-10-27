import React from 'react'
import { createRoot } from 'react-dom/client'
import SubGmApp from './SubGmApp'
import '../styles.css'
import './sub-gm.css'

createRoot(document.getElementById('root')!).render(<React.StrictMode><SubGmApp /></React.StrictMode>)
