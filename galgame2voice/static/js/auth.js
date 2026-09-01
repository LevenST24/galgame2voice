/**
 * Console token authentication shim.
 * Loaded before all other scripts: patches window.fetch so that any 401
 * response prompts for the console token, persists it to localStorage,
 * and retries the original request once with an Authorization header.
 */
(function () {
    'use strict';
    var TOKEN_KEY = 'g2v_console_token';
    var originalFetch = window.fetch.bind(window);

    function getToken() {
        try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
    }

    function saveToken(token) {
        try { localStorage.setItem(TOKEN_KEY, token); } catch (e) { /* ignore */ }
    }

    function isAuthedRequest(input) {
        try {
            var url = typeof input === 'string' ? input : (input && input.url) || '';
            return url.startsWith('/') || url.includes(location.host);
        } catch (e) { return false; }
    }

    window.g2vLogout = function () {
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
    };

    window.fetch = function (input, init) {
        var token = getToken();
        var headers = new Headers((init && init.headers) || (input && input.headers) || {});
        if (token && !headers.has('Authorization')) {
            headers.set('Authorization', 'Bearer ' + token);
        }
        var nextInit = Object.assign({}, init, { headers: headers });

        return originalFetch(input, nextInit).then(function (response) {
            if (response.status !== 401 || !isAuthedRequest(input)) {
                return response;
            }
            var entered = window.prompt('请输入控制台访问 Token（首次可从服务启动日志中获取）:', '');
            if (entered === null) {
                return response;
            }
            var trimmed = entered.trim();
            if (!trimmed) {
                return response;
            }
            saveToken(trimmed);
            var retryHeaders = new Headers((init && init.headers) || (input && input.headers) || {});
            retryHeaders.set('Authorization', 'Bearer ' + trimmed);
            var retryInit = Object.assign({}, init, { headers: retryHeaders });
            return originalFetch(input, retryInit);
        });
    };
})();
