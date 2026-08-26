import ReactDOM from 'react-dom/client'
import App from './App'
import { BalanceProvider } from './contexts/BalanceContext'
import './global.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  // <React.StrictMode>
    <BalanceProvider>
      <App />
    </BalanceProvider>
  // </React.StrictMode>,
)
