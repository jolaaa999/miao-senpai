import { createRouter, createWebHistory } from 'vue-router'
import { hasToken } from '@/api/client'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('@/components/AppLayout.vue'),
      children: [
        { path: '', name: 'dashboard', component: () => import('@/views/DashboardView.vue') },
        { path: 'sessions', name: 'sessions', component: () => import('@/views/SessionsView.vue') },
        { path: 'checkin', name: 'checkin', component: () => import('@/views/CheckinView.vue') },
        { path: 'affection', name: 'affection', component: () => import('@/views/AffectionView.vue') },
        { path: 'stickers', name: 'stickers', component: () => import('@/views/StickersView.vue') },
        { path: 'draw-gallery', name: 'draw-gallery', component: () => import('@/views/DrawGalleryView.vue') },
        { path: 'features', name: 'features', component: () => import('@/views/FeaturesView.vue') },
        { path: 'group-features', name: 'group-features', component: () => import('@/views/GroupFeaturesView.vue') },
        { path: 'persona', name: 'persona', component: () => import('@/views/PersonaView.vue') },
        { path: 'speak-styles', name: 'speak-styles', component: () => import('@/views/SpeakStylesView.vue') },
        { path: 'logs', name: 'logs', component: () => import('@/views/LogsView.vue') },
      ],
    },
  ],
})

router.beforeEach((to) => {
  if (!to.meta.public && !hasToken()) {
    return { name: 'login' }
  }
  if (to.name === 'login' && hasToken()) {
    return { name: 'dashboard' }
  }
})

export default router
