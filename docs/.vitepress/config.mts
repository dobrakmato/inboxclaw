import {defineConfig} from 'vitepress'

// https://vitepress.dev/reference/site-config
export default defineConfig({
    title: "Inboxclaw",
    description: "Unified inbox system for your openclaw instance",
    base: '/inboxclaw/',

    themeConfig: {
        logo: './assets/logo.png',
        // https://vitepress.dev/reference/default-theme-config
        nav: [
            {text: 'Home', link: '/'},
            {text: 'Use Cases', link: '/use-cases'},
        ],
        search: {
             provider: 'local'
        },
        editLink: {
            pattern: 'https://github.com/dobrakmato/inboxclaw/edit/master/docs/:path',
            text: 'Edit this page on GitHub',
        },

        sidebar: [
            {
                text: 'Onboarding Tutorial',
                items: [
                    {text: 'Overview', link: '/onboarding/'},
                    {text: '1. Install & Run', link: '/onboarding/step-1-install'},
                    {text: '2. Configure Sources', link: '/onboarding/step-2-sources'},
                    {text: '3. Configure Sinks', link: '/onboarding/step-3-sinks'},
                    {text: '4. Run Your Pipeline', link: '/onboarding/step-4-run'},
                    {text: '5. Maintenance', link: '/onboarding/step-5-maintenance'},
                ]
            },
            {
                text: 'Guides',
                items: [
                    {text: 'Inboxclaw + OpenClaw', link: '/getting-started-openclaw'},
                ]
            },
            {
                text: 'Concepts',
                items: [
                    {text: 'Pipeline', link: '/pipeline'},
                    {text: 'Sources', link: '/sources-general'},
                    {text: 'Coalescing', link: '/coalescing'},
                    {text: 'Sinks', link: '/sinks-general'},
                ]
            },
            {
                text: 'Event Sources',
                items: [
                    {text: 'Gmail', link: '/source-gmail'},
                    {text: 'Google Calendar', link: '/source-google-calendar'},
                    {text: 'Google Drive', link: '/source-google-drive'},
                    {text: 'Fio Banka', link: '/source-fio'},
                    {text: 'Faktury Online', link: '/source-faktury-online'},
                    {text: 'Home Assistant', link: '/source-home-assistant'},
                    {text: 'GoCardless / Nordigen', link: '/source-nordigen'},
                    {text: 'Mock', link: '/source-mock'},
                ]
            },
            {
                text: 'Event Sinks',
                items: [
                    {text: 'Webhook', link: '/sink-webhook'},
                    {text: 'SSE', link: '/sink-sse'},
                    {text: 'HTTP Pull', link: '/sink-http-pull'},
                    {text: 'Bash Command', link: '/sink-command'},
                    {text: 'Win11 Toast', link: '/sink-win11toast'},
                ]
            },
            {
                text: 'Reference',
                items: [
                    {text: 'App Lifecycle', link: '/app-lifecycle'},
                    {text: 'CLI Reference', link: '/cli'},
                    {text: 'Key/Value storage', link: '/kv-general'},
                    {text: 'Configuration', link: '/configuration'},
                    {text: 'Templating', link: '/templating'},
                    {text: 'Data model', link: '/data-model'},
                    {text: 'Google Auth CLI', link: '/google-auth-cli'},
                ]
            }
        ],

        socialLinks: [
            {icon: 'github', link: 'https://github.com/dobrakmato/inboxclaw'}
        ]
    }
})
