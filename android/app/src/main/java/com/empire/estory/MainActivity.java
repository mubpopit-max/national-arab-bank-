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
import android.os.Handler;
import android.os.Looper;
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
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

public final class MainActivity extends Activity {
    private static final String HOST = "national-arab-bank-d2siv.faable.link";
    private static final String START_URL = "https://" + HOST + "/store/";
    private static final String UPDATE_URL = "https://raw.githubusercontent.com/mubpopit-max/national-arab-bank-/main/android/update.json";
    private static final int FILE_CHOOSER_REQUEST = 4107;

    private WebView webView;
    private ValueCallback<Uri[]> fileChooserCallback;
    private int systemBottomInset = 0;
    private DownloadManager downloadManager;
    private long pendingDownloadId = -1L;
    private BroadcastReceiver downloadReceiver;
    private String pendingApkUrl;
    private boolean updateDialogVisible = false;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window window = getWindow();
        window.setStatusBarColor(Color.WHITE);
        window.setNavigationBarColor(Color.WHITE);
        window.getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        );
        downloadManager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
        checkForMandatoryUpdate();
    }

    private void checkForMandatoryUpdate() {
        new Thread(() -> {
            HttpURLConnection connection = null;
            try {
                connection = (HttpURLConnection) new URL(UPDATE_URL).openConnection();
                connection.setConnectTimeout(10000);
                connection.setReadTimeout(10000);
                connection.setRequestMethod("GET");
                connection.setUseCaches(false);
                int status = connection.getResponseCode();
                if (status != HttpURLConnection.HTTP_OK) throw new IllegalStateException("Update check failed");
                StringBuilder body = new StringBuilder();
                try (BufferedReader reader = new BufferedReader(new InputStreamReader(connection.getInputStream(), "UTF-8"))) {
                    String line;
                    while ((line = reader.readLine()) != null) body.append(line);
                }
                JSONObject update = new JSONObject(body.toString());
                int latestCode = update.getInt("versionCode");
                String latestName = update.optString("versionName", "");
                String apkUrl = update.getString("apkUrl");
                boolean mandatory = update.optBoolean("mandatory", true);
                if (apkUrl == null || !apkUrl.startsWith("https://")) throw new IllegalStateException("Invalid APK URL");
                if (latestCode > BuildConfig.VERSION_CODE && mandatory) {
                    pendingApkUrl = apkUrl;
                    mainHandler.post(() -> showMandatoryUpdate(latestName));
                } else {
                    mainHandler.post(this::openStore);
                }
            } catch (Exception error) {
                mainHandler.post(() -> showUpdateCheckError());
            } finally {
                if (connection != null) connection.disconnect();
            }
        }).start();
    }

    private void showUpdateCheckError() {
        new AlertDialog.Builder(this)
            .setTitle("تعذر التحقق من التحديث")
            .setMessage("يلزم الاتصال بالإنترنت والتحقق من إصدار التطبيق قبل المتابعة.")
            .setCancelable(false)
            .setPositiveButton("إعادة المحاولة", (dialog, which) -> checkForMandatoryUpdate())
            .setNegativeButton("إغلاق", (dialog, which) -> finish())
            .show();
    }

    private void showMandatoryUpdate(String versionName) {
        if (isFinishing() || updateDialogVisible) return;
        updateDialogVisible = true;
        new AlertDialog.Builder(this)
            .setTitle("تحديث إلزامي متوفر")
            .setMessage("يتوفر إصدار جديد " + versionName + " من Empire E-Story. يجب تثبيته لمتابعة استخدام التطبيق.")
            .setCancelable(false)
            .setPositiveButton("تحميل وتثبيت التحديث", (dialog, which) -> startApkDownload())
            .setNegativeButton("إغلاق التطبيق", (dialog, which) -> finish())
            .setOnDismissListener(dialog -> updateDialogVisible = false)
            .show();
    }

    private void startApkDownload() {
        if (pendingApkUrl == null) {
            checkForMandatoryUpdate();
            return;
        }
        try {
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(pendingApkUrl));
            request.setTitle("تحديث Empire E-Story");
            request.setDescription("جارٍ تنزيل الإصدار الجديد");
            request.setMimeType("application/vnd.android.package-archive");
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, "empire-estory-update.apk");
            pendingDownloadId = downloadManager.enqueue(request);
            registerDownloadReceiver();
            Toast.makeText(this, "بدأ تحميل التحديث. افتح إشعار التنزيل عند اكتماله إذا لم يبدأ التثبيت تلقائياً.", Toast.LENGTH_LONG).show();
        } catch (Exception error) {
            new AlertDialog.Builder(this)
                .setTitle("تعذر تنزيل التحديث")
                .setMessage("تحقق من اتصال الإنترنت ثم أعد المحاولة.")
                .setCancelable(false)
                .setPositiveButton("إعادة المحاولة", (d, w) -> startApkDownload())
                .setNegativeButton("إغلاق", (d, w) -> finish())
                .show();
        }
    }

    private void registerDownloadReceiver() {
        if (downloadReceiver != null) return;
        downloadReceiver = new BroadcastReceiver() {
            @Override public void onReceive(Context context, Intent intent) {
                long id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L);
                if (id != pendingDownloadId) return;
                DownloadManager.Query query = new DownloadManager.Query().setFilterById(id);
                try (android.database.Cursor cursor = downloadManager.query(query)) {
                    if (cursor == null || !cursor.moveToFirst()) {
                        showDownloadFailure();
                        return;
                    }
                    int status = cursor.getInt(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
                    if (status == DownloadManager.STATUS_SUCCESSFUL) {
                        Uri apkUri = downloadManager.getUriForDownloadedFile(id);
                        if (apkUri != null) installApk(apkUri);
                        else showDownloadFailure();
                    } else if (status == DownloadManager.STATUS_FAILED) {
                        showDownloadFailure();
                    }
                } catch (Exception error) {
                    showDownloadFailure();
                }
            }
        };
        IntentFilter filter = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(downloadReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(downloadReceiver, filter);
        }
    }

    private void installApk(Uri apkUri) {
        if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
            Intent settings = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:" + getPackageName()));
            startActivity(settings);
            Toast.makeText(this, "اسمح لهذا التطبيق بتثبيت التحديث، ثم ارجع إليه.", Toast.LENGTH_LONG).show();
            return;
        }
        try {
            Intent install = new Intent(Intent.ACTION_VIEW);
            install.setDataAndType(apkUri, "application/vnd.android.package-archive");
            install.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            install.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(install);
        } catch (ActivityNotFoundException error) {
            Toast.makeText(this, "تعذر فتح مثبت Android.", Toast.LENGTH_LONG).show();
        }
    }

    private void showDownloadFailure() {
        new AlertDialog.Builder(this)
            .setTitle("فشل تحميل التحديث")
            .setMessage("لم يكتمل تنزيل ملف التحديث. حاول مرة أخرى.")
            .setCancelable(false)
            .setPositiveButton("إعادة المحاولة", (d, w) -> startApkDownload())
            .setNegativeButton("إغلاق التطبيق", (d, w) -> finish())
            .show();
    }

    private void openStore() {
        WebView view = new WebView(this);
        webView = view;
        view.setBackgroundColor(Color.WHITE);
        view.setOnApplyWindowInsetsListener((v, insets) -> {
            int left, top, right, bottom;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
                left = bars.left; top = bars.top; right = bars.right; bottom = bars.bottom;
            } else {
                left = insets.getSystemWindowInsetLeft(); top = insets.getSystemWindowInsetTop();
                right = insets.getSystemWindowInsetRight(); bottom = insets.getSystemWindowInsetBottom();
            }
            systemBottomInset = bottom;
            v.setPadding(left, top, right, 0);
            v.post(this::applyWebInsets);
            return insets;
        });
        setContentView(view);

        WebSettings settings = view.getSettings();
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
        cookies.setAcceptThirdPartyCookies(view, false);
        view.setWebViewClient(new SecureWebViewClient());
        view.setWebChromeClient(new SecureWebChromeClient());
        view.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            Uri uri = Uri.parse(url);
            if ("https".equalsIgnoreCase(uri.getScheme())) openExternal(uri);
        });
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        view.loadUrl(START_URL);
    }

    private void applyWebInsets() {
        if (webView == null) return;
        String script = "document.documentElement.style.setProperty('--native-bottom-inset','" + systemBottomInset + "px');";
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
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); }
        catch (ActivityNotFoundException ignored) {
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
        @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                                    FileChooserParams params) {
            if (fileChooserCallback != null) fileChooserCallback.onReceiveValue(null);
            fileChooserCallback = callback;
            Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("image/*");
            try {
                startActivityForResult(Intent.createChooser(intent, "اختر صورة"), FILE_CHOOSER_REQUEST);
            } catch (ActivityNotFoundException error) {
                fileChooserCallback = null;
                callback.onReceiveValue(null);
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
        if (downloadReceiver != null) {
            try { unregisterReceiver(downloadReceiver); } catch (IllegalArgumentException ignored) {}
            downloadReceiver = null;
        }
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
