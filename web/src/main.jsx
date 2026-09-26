import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './styles.css'
import { API_BASE_URL } from './config.js'

console.log('API_BASE_URL', API_BASE_URL)
createRoot(document.getElementById('root')).render(<App />)
