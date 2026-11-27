package com.empire.estory;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Insets;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.Window;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.net.http.SslError;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

public final class MainActivity extends Activity {
    private static final String HOST = "national-arab-bank-d2siv.faable.link";
    private static final String START_URL = "https://" + HOST + "/store/";
    private static final int FILE_CHOOSER_REQUEST = 4107;

    private WebView webView;
    private ValueCallback<Uri[]> fileChooserCallback;
    private int systemBottomInset = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window window = getWindow();
        window.setStatusBarColor(Color.WHITE);
        window.setNavigationBarColor(Color.WHITE);
        window.getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        );

        webView = new WebView(this);
        webView.setBackgroundColor(Color.WHITE);
        webView.setOnApplyWindowInsetsListener((view, insets) -> {
            int left;
            int top;
            int right;
            int bottom;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
                left = bars.left;
                top = bars.top;
                right = bars.right;
                bottom = bars.bottom;
            } else {
                left = insets.getSystemWindowInsetLeft();
                top = insets.getSystemWindowInsetTop();
                right = insets.getSystemWindowInsetRight();
                bottom = insets.getSystemWindowInsetBottom();
            }
            systemBottomInset = bottom;
            view.setPadding(left, top, right, 0);
            view.post(() -> applyWebInsets());
            return insets;
        });
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
        }

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);

        webView.setWebViewClient(new SecureWebViewClient());
        webView.setWebChromeClient(new SecureWebChromeClient());
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            Uri downloadUri = Uri.parse(url);
            if ("https".equalsIgnoreCase(downloadUri.getScheme())) {
                openExternal(downloadUri);
            }
        });
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);

        if (savedInstanceState != null && webView.restoreState(savedInstanceState) != null) {
            return;
        }
        webView.loadUrl(START_URL);
    }

    private void applyWebInsets() {
        if (webView == null) return;
        String script = "document.documentElement.style.setProperty('--native-bottom-inset','"
            + systemBottomInset + "px');";
        webView.evaluateJavascript(script, null);
    }

    private boolean handleNavigation(Uri uri) {
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
        String host = uri.getHost();

        if ("https".equals(scheme) && HOST.equalsIgnoreCase(host)) {
            return false;
        }

        if ("https".equals(scheme)) {
            openExternal(uri);
            return true;
        }

        if ("mailto".equals(scheme) || "tel".equals(scheme)) {
            openExternal(uri);
            return true;
        }

        // Never allow cleartext HTTP, file:, content:, javascript: or unknown schemes.
        return true;
    }

    private void openExternal(Uri uri) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, uri);
            startActivity(intent);
        } catch (ActivityNotFoundException ignored) {
            Toast.makeText(this, "لا يوجد تطبيق مناسب لفتح الرابط.", Toast.LENGTH_SHORT).show();
        }
    }

    private final class SecureWebViewClient extends WebViewClient {
        @Override
        public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            applyWebInsets();
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return handleNavigation(request.getUrl());
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            return handleNavigation(Uri.parse(url));
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            handler.cancel();
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                String html = "<!doctype html><html lang='ar' dir='rtl'><meta charset='utf-8'>"
                    + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    + "<body style='background:#090909;color:#f6f1e6;font-family:sans-serif;text-align:center;padding:48px 20px'>"
                    + "<h2 style='color:#d8b45e'>تعذر الاتصال بالمتجر</h2>"
                    + "<p>تحقق من اتصال الإنترنت ثم حاول مجدداً.</p>"
                    + "<button style='padding:12px 24px;background:#d8b45e;border:0;border-radius:8px' "
                    + "onclick=\"location.href='" + START_URL + "'\">إعادة المحاولة</button></body></html>";
                view.loadDataWithBaseURL(START_URL, html, "text/html", "UTF-8", null);
            }
        }
    }

    private final class SecureWebChromeClient extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(
            WebView view,
            ValueCallback<Uri[]> filePathCallback,
            FileChooserParams fileChooserParams
        ) {
            if (fileChooserCallback != null) {
                fileChooserCallback.onReceiveValue(null);
            }
            fileChooserCallback = filePathCallback;

            Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("image/*");

            try {
                startActivityForResult(Intent.createChooser(intent, "اختر صورة"), FILE_CHOOSER_REQUEST);
            } catch (ActivityNotFoundException error) {
                fileChooserCallback = null;
                filePathCallback.onReceiveValue(null);
                Toast.makeText(MainActivity.this, "تعذر فتح معرض الصور.", Toast.LENGTH_SHORT).show();
                return false;
            }
            return true;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQUEST && fileChooserCallback != null) {
            Uri[] result = null;
            if (resultCode == RESULT_OK && data != null && data.getData() != null) {
                result = new Uri[] { data.getData() };
            }
            fileChooserCallback.onReceiveValue(result);
            fileChooserCallback = null;
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        if (webView != null) {
            webView.saveState(outState);
        }
        super.onSaveInstanceState(outState);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (fileChooserCallback != null) {
            fileChooserCallback.onReceiveValue(null);
            fileChooserCallback = null;
        }
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
