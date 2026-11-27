package com.empire.estory;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Color;
import android.graphics.Insets;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.Settings;
import android.view.View;
import android.view.Window;
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

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final String HOST = "national-arab-bank-d2siv.faable.link";
    private static final String START_URL = "https://" + HOST + "/store/";
    private static final String UPDATE_URL =
        "https://raw.githubusercontent.com/mubpopit-max/national-arab-bank-/main/android/update.json";
    private static final String PREFS = "empire_update_state";
    private static final String PENDING_URL = "pending_apk_url";
    private static final int FILE_CHOOSER_REQUEST = 4107;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private WebView webView;
    private ValueCallback<Uri[]> fileChooserCallback;
    private int systemBottomInset = 0;
    private long activeDownloadId = -1L;
    private boolean receiverRegistered = false;

    private final BroadcastReceiver downloadReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (!DownloadManager.ACTION_DOWNLOAD_COMPLETE.equals(intent.getAction())) return;
            long id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L);
            if (id != activeDownloadId) return;

            DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            activeDownloadId = -1L;
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().remove(PENDING_URL).apply();
            Uri apkUri = manager.getUriForDownloadedFile(id);
            if (apkUri == null) {
                showBlockingError("تعذر تنزيل ملف التحديث كاملاً. أعد المحاولة.");
                return;
            }
            installDownloadedApk(apkUri);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window window = getWindow();
        window.setStatusBarColor(Color.WHITE);
        window.setNavigationBarColor(Color.WHITE);
        window.getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        );
        registerDownloadReceiver();
        checkForMandatoryUpdate();
    }

    private void registerDownloadReceiver() {
        IntentFilter filter = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(downloadReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(downloadReceiver, filter);
        }
        receiverRegistered = true;
    }

    private void checkForMandatoryUpdate() {
        showBlockingError("جارٍ التحقق من تحديث التطبيق...");
        executor.execute(() -> {
            HttpURLConnection connection = null;
            try {
                connection = (HttpURLConnection) new URL(UPDATE_URL).openConnection();
                connection.setRequestMethod("GET");
                connection.setConnectTimeout(12000);
                connection.setReadTimeout(12000);
                connection.setUseCaches(false);
                connection.setRequestProperty("Cache-Control", "no-cache");
                int status = connection.getResponseCode();
                if (status != HttpURLConnection.HTTP_OK) {
                    throw new IllegalStateException("Update service returned HTTP " + status);
                }
                StringBuilder body = new StringBuilder();
                try (InputStream stream = connection.getInputStream();
                     BufferedReader reader = new BufferedReader(new InputStreamReader(stream, "UTF-8"))) {
                    String line;
                    while ((line = reader.readLine()) != null) body.append(line);
                }
                JSONObject data = new JSONObject(body.toString());
                int latestCode = data.getInt("latestVersionCode");
                String latestName = data.getString("latestVersionName");
                String apkUrl = data.getString("apkUrl");
                boolean mandatory = data.optBoolean("mandatory", true);
                Uri parsed = Uri.parse(apkUrl);
                if (!"https".equalsIgnoreCase(parsed.getScheme())
                    || !"github.com".equalsIgnoreCase(parsed.getHost())
                    || !apkUrl.endsWith(".apk")) {
                    throw new IllegalStateException("Invalid APK download URL");
                }

                runOnUiThread(() -> {
                    if (latestCode > BuildConfig.VERSION_CODE && mandatory) {
                        showMandatoryUpdate(latestName, apkUrl);
                    } else {
                        openStore();
                    }
                });
            } catch (Exception error) {
                runOnUiThread(() -> showBlockingError(
                    "تعذر التحقق من إصدار التطبيق. يلزم الاتصال بالإنترنت للتحقق قبل المتابعة."
                ));
            } finally {
                if (connection != null) connection.disconnect();
            }
        });
    }

    private void showMandatoryUpdate(String versionName, String apkUrl) {
        new AlertDialog.Builder(this)
            .setTitle("تحديث إلزامي متوفر")
            .setMessage("يتوفر إصدار جديد (" + versionName + "). يجب تثبيته لمواصلة استخدام الإمبراطورية إستوري.")
            .setCancelable(false)
            .setPositiveButton("تحميل وتثبيت التحديث", (dialog, which) -> beginUpdate(apkUrl))
            .show();
    }

    private void beginUpdate(String apkUrl) {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(PENDING_URL, apkUrl).apply();
        if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
            new AlertDialog.Builder(this)
                .setTitle("السماح بالتثبيت")
                .setMessage("اسمح لهذا التطبيق بتثبيت التحديث من مصدره، ثم ارجع إلى التطبيق لإكمال التثبيت.")
                .setCancelable(false)
                .setPositiveButton("فتح الإعدادات", (dialog, which) -> {
                    Intent settings = new Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:" + getPackageName())
                    );
                    startActivity(settings);
                })
                .show();
            return;
        }
        startApkDownload(apkUrl);
    }

    @Override
    protected void onResume() {
        super.onResume();
        String pending = getSharedPreferences(PREFS, MODE_PRIVATE).getString(PENDING_URL, null);
        if (pending != null && (Build.VERSION.SDK_INT < 26 || getPackageManager().canRequestPackageInstalls())) {
            startApkDownload(pending);
        }
    }

    private void startApkDownload(String apkUrl) {
        if (activeDownloadId != -1L) return;
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().remove(PENDING_URL).apply();
        DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
        DownloadManager.Request request = new DownloadManager.Request(Uri.parse(apkUrl));
        request.setTitle("تحديث الإمبراطورية إستوري");
        request.setDescription("جارٍ تنزيل الإصدار الإلزامي");
        request.setMimeType("application/vnd.android.package-archive");
        request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        request.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, "empire-estory-update.apk");
        activeDownloadId = manager.enqueue(request);
        Toast.makeText(this, "بدأ تنزيل التحديث...", Toast.LENGTH_LONG).show();
    }

    private void installDownloadedApk(Uri apkUri) {
        if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
            Toast.makeText(this, "يجب السماح بتثبيت التحديث من هذا المصدر.", Toast.LENGTH_LONG).show();
            return;
        }
        Intent install = new Intent(Intent.ACTION_VIEW);
        install.setDataAndType(apkUri, "application/vnd.android.package-archive");
        install.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            startActivity(install);
        } catch (ActivityNotFoundException error) {
            showBlockingError("لم يتم العثور على مثبت حزم Android على الجهاز.");
        }
    }

    private void showBlockingError(String message) {
        new AlertDialog.Builder(this)
            .setTitle("الإمبراطورية إستوري")
            .setMessage(message)
            .setCancelable(false)
            .setPositiveButton("إعادة المحاولة", (dialog, which) -> checkForMandatoryUpdate())
            .show();
    }

    private void openStore() {
        initializeWebView();
        webView.loadUrl(START_URL);
    }

    private void initializeWebView() {
        if (webView != null) return;
        webView = new WebView(this);
        webView.setBackgroundColor(Color.WHITE);
        webView.setOnApplyWindowInsetsListener((view, insets) -> {
            int left, top, right, bottom;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
                left = bars.left; top = bars.top; right = bars.right; bottom = bars.bottom;
            } else {
                left = insets.getSystemWindowInsetLeft(); top = insets.getSystemWindowInsetTop();
                right = insets.getSystemWindowInsetRight(); bottom = insets.getSystemWindowInsetBottom();
            }
            systemBottomInset = bottom;
            view.setPadding(left, top, right, 0);
            view.post(this::applyWebInsets);
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
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) settings.setSafeBrowsingEnabled(true);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);
        webView.setWebViewClient(new SecureWebViewClient());
        webView.setWebChromeClient(new SecureWebChromeClient());
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            Uri downloadUri = Uri.parse(url);
            if ("https".equalsIgnoreCase(downloadUri.getScheme())) openExternal(downloadUri);
        });
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
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
        if ("https".equals(scheme) && HOST.equalsIgnoreCase(host)) return false;
        if ("https".equals(scheme) || "mailto".equals(scheme) || "tel".equals(scheme)) {
            openExternal(uri);
            return true;
        }
        return true;
    }

    private void openExternal(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException ignored) {
            Toast.makeText(this, "لا يوجد تطبيق مناسب لفتح الرابط.", Toast.LENGTH_SHORT).show();
        }
    }

    private final class SecureWebViewClient extends WebViewClient {
        @Override public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            applyWebInsets();
        }
        @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return handleNavigation(request.getUrl());
        }
        @Override public boolean shouldOverrideUrlLoading(WebView view, String url) {
            return handleNavigation(Uri.parse(url));
        }
        @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            handler.cancel();
        }
        @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
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
        @Override public boolean onShowFileChooser(
            WebView view, ValueCallback<Uri[]> filePathCallback, FileChooserParams fileChooserParams
        ) {
            if (fileChooserCallback != null) fileChooserCallback.onReceiveValue(null);
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

    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQUEST && fileChooserCallback != null) {
            Uri[] result = null;
            if (resultCode == RESULT_OK && data != null && data.getData() != null) result = new Uri[]{data.getData()};
            fileChooserCallback.onReceiveValue(result);
            fileChooserCallback = null;
        }
    }

    @Override protected void onSaveInstanceState(Bundle outState) {
        if (webView != null) webView.saveState(outState);
        super.onSaveInstanceState(outState);
    }

    @Override public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override protected void onDestroy() {
        if (receiverRegistered) {
            try { unregisterReceiver(downloadReceiver); } catch (IllegalArgumentException ignored) {}
        }
        executor.shutdownNow();
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
