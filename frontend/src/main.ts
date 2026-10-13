import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

// 应用只装配一个根视图，页面状态和 API 交互集中在工作台入口维护。
createApp(App).mount('#app')
