(function() {
        // 1. Override Page Visibility API (Bypasses auto-pause on background tabs / unfocused window)
        try {
            Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
            Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
            window.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
        } catch(e) {}

        // 2. Direct Style Injection for guaranteed CSS application across all frames
        const cssRules = `
            /* 1. Permanent Suppress for Ads, Banners, Warnings, Notices, Cookies & Consents (Universal Wildcard) */
            [class*='warning' i], [id*='warning' i],
            [class*='alert' i], [id*='alert' i],
            [class*='banner' i], [id*='banner' i],
            [class*='notice' i], [id*='notice' i],
            [class*='cookie' i], [id*='cookie' i],
            [class*='consent' i], [id*='consent' i],
            [class*='ad-' i], [class*='ads-' i], [id*='ad-' i], [id*='ads-' i],
            [id*='google_ads_iframe' i], .rmp-ad-container, .ima-ad-container, #ad-container {
                display: none !important;
                opacity: 0 !important;
                visibility: hidden !important;
                pointer-events: none !important;
                height: 0 !important;
                min-height: 0 !important;
                max-height: 0 !important;
                margin: 0 !important;
                padding: 0 !important;
                overflow: hidden !important;
            }

            /* 2. Universal Navigation, Header, Menu, Logo, Search Auto-Hide when streaming */
            body.cc-hide-nav header,
            body.cc-hide-nav nav:not(.sidebar):not([class*='sidebar']):not(aside),
            body.cc-hide-nav footer,
            body.cc-hide-nav aside:not(.sidebar):not([class*='sidebar']),
            body.cc-hide-nav [class*='header' i]:not(.video-js):not([class*='player']):not(#videoPlayer),
            body.cc-hide-nav [id*='header' i]:not(.video-js):not([id*='player']):not(#videoPlayer),
            body.cc-hide-nav [class*='navbar' i]:not(.video-js):not([class*='player']),
            body.cc-hide-nav [id*='navbar' i]:not(.video-js):not([id*='player']),
            body.cc-hide-nav [class*='topbar' i]:not(.video-js):not([class*='player']),
            body.cc-hide-nav [id*='topbar' i]:not(.video-js):not([id*='player']),
            body.cc-hide-nav [class*='site-logo' i],
            body.cc-hide-nav [class*='logo' i]:not(.video-js):not([class*='player']):not(.sidebar):not([class*='sidebar']),
            body.cc-hide-nav [id*='logo' i]:not(.video-js):not([id*='player']):not(.sidebar):not([class*='sidebar']),
            body.cc-hide-nav [class*='brand' i]:not(.video-js):not([class*='player']):not(.sidebar):not([class*='sidebar']),
            body.cc-hide-nav [id*='brand' i]:not(.video-js):not([id*='player']):not(.sidebar):not([class*='sidebar']),
            body.cc-hide-nav [class*='search' i]:not(.video-js):not([class*='player']):not(.sidebar):not([class*='sidebar']),
            body.cc-hide-nav [id*='search' i]:not(.video-js):not([id*='player']):not(.sidebar):not([class*='sidebar']),
            body.cc-hide-nav [class*='notif' i]:not(.video-js):not([class*='player']),
            body.cc-hide-nav [id*='notif' i]:not(.video-js):not([id*='player']),
            body.cc-hide-nav [class*='profile' i]:not(.video-js):not([class*='player']),
            body.cc-hide-nav [id*='profile' i]:not(.video-js):not([id*='player']),
            body.cc-hide-nav [class*='account' i]:not(.video-js):not([class*='player']),
            body.cc-hide-nav [id*='account' i]:not(.video-js):not([id*='player']) {
                opacity: 0 !important;
                visibility: hidden !important;
                pointer-events: none !important;
                transition: opacity 0.3s ease-in-out, visibility 0.3s !important;
            }

            /* 3. Sidebars and category menus are ALWAYS visible and clickable */
            .sidebar,
            aside,
            [class*='sidebar' i],
            [id*='sidebar' i],
            .sidebar *,
            aside *,
            [class*='sidebar' i] *,
            [id*='sidebar' i] * {
                opacity: 1 !important;
                visibility: visible !important;
                pointer-events: auto !important;
            }

            /* 4. Reveal when user moves mouse to top or interacts */
            body:not(.cc-hide-nav) header,
            body:not(.cc-hide-nav) nav,
            body:not(.cc-hide-nav) [class*='header' i],
            body:not(.cc-hide-nav) [id*='header' i],
            body:not(.cc-hide-nav) [class*='navbar' i],
            body:not(.cc-hide-nav) [id*='navbar' i],
            body:not(.cc-hide-nav) [class*='topbar' i],
            body:not(.cc-hide-nav) [id*='topbar' i],
            body:not(.cc-hide-nav) [class*='menu' i],
            body:not(.cc-hide-nav) [id*='menu' i],
            body:not(.cc-hide-nav) [class*='nav' i],
            body:not(.cc-hide-nav) [id*='nav' i],
            body:not(.cc-hide-nav) [class*='logo' i],
            body:not(.cc-hide-nav) [id*='logo' i],
            body:not(.cc-hide-nav) [class*='brand' i],
            body:not(.cc-hide-nav) [id*='brand' i],
            body:not(.cc-hide-nav) [class*='search' i],
            body:not(.cc-hide-nav) [id*='search' i],
            body:not(.cc-hide-nav) [class*='notif' i],
            body:not(.cc-hide-nav) [id*='notif' i],
            body:not(.cc-hide-nav) [class*='profile' i],
            body:not(.cc-hide-nav) [id*='profile' i],
            body:not(.cc-hide-nav) [class*='account' i],
            body:not(.cc-hide-nav) [id*='account' i] {
                opacity: 1 !important;
                visibility: visible !important;
                pointer-events: auto !important;
                z-index: 100001 !important;
            }
        `;

        function injectStyle() {
            if (!document.getElementById('cc-injected-styles')) {
                const s = document.createElement('style');
                s.id = 'cc-injected-styles';
                s.textContent = cssRules;
                (document.head || document.documentElement).appendChild(s);
            }
        }
        try { injectStyle(); } catch(e) {}
        window.addEventListener('DOMContentLoaded', injectStyle);

        // 3. Universal Video Detection & Full-Window Scaling (with 3-4s delay)
        let playSeconds = 0;
        let idleTimer = null;
        let activeFullscreenEl = null;

        function findVideoContainer(videoEl) {
            if (!videoEl) return null;
            let bestContainer = videoEl;
            let current = videoEl.parentElement;
            let depth = 0;
            while (current && current !== document.body && current !== document.documentElement && depth < 6) {
                const id = (current.id || '').toLowerCase();
                const cls = (current.className || '').toString().toLowerCase();
                if (
                    id === 'full-screen-closed' ||
                    id === 'videoplayer' ||
                    id === 'video__wrapper' ||
                    id.includes('player') ||
                    id.includes('video') ||
                    cls.includes('video-js') ||
                    cls.includes('vjs') ||
                    cls.includes('player') ||
                    cls.includes('video') ||
                    cls.includes('rmp')
                ) {
                    bestContainer = current;
                }
                current = current.parentElement;
                depth++;
            }
            return bestContainer;
        }

        function checkVideo() {
            const videos = Array.from(document.querySelectorAll('video'));
            let isPlaying = false;
            let playingVideo = null;

            for (const v of videos) {
                // Check if video is playing: not paused, not ended, and has either started playback or is active
                if (!v.paused && !v.ended && (v.currentTime > 0 || v.readyState >= 1 || v.seeking || v.duration > 0 || v.classList.contains('vjs-tech'))) {
                    isPlaying = true;
                    playingVideo = v;
                    break;
                }
            }

            if (isPlaying && playingVideo) {
                playSeconds += 1;
            } else {
                playSeconds = 0;
                if (activeFullscreenEl) {
                    activeFullscreenEl.classList.remove('cc-full-window-player');
                    activeFullscreenEl = null;
                }
            }

            // Only auto-hide headers and scale after 3 seconds of confirmed continuous playback
            if (playSeconds >= 3 && document.body) {
                document.body.classList.add('cc-hide-nav');
                if (playingVideo) {
                    const container = findVideoContainer(playingVideo);
                    if (container && container !== activeFullscreenEl) {
                        if (activeFullscreenEl) {
                            activeFullscreenEl.classList.remove('cc-full-window-player');
                        }
                        container.classList.add('cc-full-window-player');
                        activeFullscreenEl = container;
                    } else if (container) {
                        container.classList.add('cc-full-window-player');
                    }
                }
            } else if (document.body) {
                document.body.classList.remove('cc-hide-nav');
            }
        }

        const UI_WILDCARD_SELECTOR = 'header, nav, footer, [class*="header" i], [id*="header" i], [class*="navbar" i], [id*="navbar" i], [class*="topbar" i], [id*="topbar" i], [class*="site-logo" i], [class*="search" i], [id*="search" i], [class*="notif" i], [id*="notif" i], [class*="profile" i], [id*="profile" i], [class*="account" i], [id*="account" i]';
        const BANNER_WILDCARD_SELECTOR = '[class*="warning" i], [id*="warning" i], [class*="alert" i], [id*="alert" i], [class*="banner" i], [id*="banner" i], [class*="notice" i], [id*="notice" i], [class*="cookie" i], [id*="cookie" i], [class*="consent" i], [id*="consent" i]';

        // 4. Active UI Cleanup (Removes warning banners & suppresses hidden elements)
        function sweepUI() {
            // Remove warning/alert/cookie banners outside video player and sidebar
            document.querySelectorAll(BANNER_WILDCARD_SELECTOR).forEach(el => {
                if (el.closest && el.closest('.cc-full-window-player, .video-js, #video__wrapper, #full-screen-closed, .sidebar, aside, [class*="sidebar" i], [id*="sidebar" i]')) return;
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('opacity', '0', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
                el.style.setProperty('height', '0px', 'important');
            });

            // Suppress top headers & logos if streaming
            const isStreaming = playSeconds >= 3 && document.body && document.body.classList.contains('cc-hide-nav');
            document.querySelectorAll(UI_WILDCARD_SELECTOR).forEach(el => {
                // Never hide video elements, player wrappers, controls, or sidebar/category menus
                if (el.tagName === 'VIDEO' || el.tagName === 'CANVAS' || el.id === 'videoPlayer' || el.id === 'video__wrapper' || el.classList.contains('video-js') || el.classList.contains('vjs-tech') || (el.closest && el.closest('.cc-full-window-player, .video-js, #video__wrapper, #full-screen-closed, .sidebar, aside, [class*="sidebar" i], [id*="sidebar" i]'))) {
                    return;
                }
                if (isStreaming) {
                    el.style.setProperty('opacity', '0', 'important');
                    el.style.setProperty('visibility', 'hidden', 'important');
                    el.style.setProperty('pointer-events', 'none', 'important');
                } else {
                    el.style.removeProperty('opacity');
                    el.style.removeProperty('visibility');
                    el.style.removeProperty('pointer-events');
                }
            });
        }

        // 5. User Activity: Reveal headers when mouse moves near top edge (clientY < 80) or left edge (clientX < 100) or on keypress
        let lastX = -1, lastY = -1;
        function handleUserActivity(e) {
            if (e && e.type === 'mousemove') {
                if (e.clientX === lastX && e.clientY === lastY) return;
                lastX = e.clientX;
                lastY = e.clientY;
                // If video is playing and mouse is not at top or left edge, do not reveal
                if (e.clientY >= 80 && e.clientX >= 100 && playSeconds >= 3) {
                    return;
                }
            }

            if (document.body && document.body.classList.contains('cc-hide-nav')) {
                document.body.classList.remove('cc-hide-nav');
                sweepUI();
            }
            if (idleTimer) clearTimeout(idleTimer);
            idleTimer = setTimeout(() => {
                if (playSeconds >= 3 && document.body) {
                    document.body.classList.add('cc-hide-nav');
                    sweepUI();
                }
            }, 3000);
        }

        window.addEventListener('mousemove', handleUserActivity, { passive: true });
        window.addEventListener('mousedown', handleUserActivity, { passive: true });
        window.addEventListener('keydown', handleUserActivity, { passive: true });

        // 6. Auto-accept First Run / Onboarding Dialogs
        function autoAcceptPrompts() {
            document.querySelectorAll('button, a').forEach(b => {
                const txt = (b.textContent || '').trim().toLowerCase();
                if (txt === 'continue' || txt === 'agree' || txt === 'accept' || txt === 'get started') {
                    if (document.body && (document.body.textContent.includes('Welcome to Firefox') || document.body.textContent.includes('Terms of Use'))) {
                        try { b.click(); } catch(e) {}
                    }
                }
            });
        }

        setInterval(() => {
            checkVideo();
            sweepUI();
            injectStyle();
        }, 1000);
        setInterval(autoAcceptPrompts, 2000);
    })();