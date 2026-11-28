package com.empire.estory;

import android.app.Activity;
import android.os.Bundle;
import android.graphics.Color;
import android.graphics.Typeface;
import android.content.SharedPreferences;
import android.view.Gravity;
import android.widget.*;
import org.json.JSONObject;
import org.json.JSONArray;
import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final String API="https://national-arab-bank-d2siv.faable.link/store/api/v1";
    private static final String PREF="empire_native", KEY="token";
    private static final int BG=0xff090909, PANEL=0xff191919, GOLD=0xffd8b45e, WHITE=0xfff6f1e6;
    private final ExecutorService worker=Executors.newSingleThreadExecutor();
    private LinearLayout root, body;
    private String token="";

    @Override public void onCreate(Bundle state){super.onCreate(state);getWindow().setStatusBarColor(BG);getWindow().setNavigationBarColor(BG);
        token=getSharedPreferences(PREF,MODE_PRIVATE).getString(KEY,""); if(token.isEmpty())login();else dashboard();}
    private void shell(String title){root=new LinearLayout(this);root.setOrientation(1);root.setBackgroundColor(BG);
        TextView brand=label("♛  الإمبراطورية إستوري",21,GOLD,true);brand.setGravity(Gravity.CENTER);brand.setPadding(12,20,12,16);root.addView(brand);
        TextView h=label(title,19,WHITE,true);h.setPadding(20,8,20,12);root.addView(h);
        ScrollView sc=new ScrollView(this);body=new LinearLayout(this);body.setOrientation(1);body.setPadding(16,8,16,24);sc.addView(body);
        root.addView(sc,new LinearLayout.LayoutParams(-1,0,1));setContentView(root);}
    private TextView label(String s,int size,int color,boolean bold){TextView t=new TextView(this);t.setText(s);t.setTextSize(size);t.setTextColor(color);if(bold)t.setTypeface(null,Typeface.BOLD);t.setPadding(8,7,8,7);return t;}
    private void card(String title,String value){LinearLayout c=new LinearLayout(this);c.setOrientation(1);c.setPadding(14,10,14,10);c.setBackgroundColor(PANEL);
        LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=9;body.addView(c,p);c.addView(label(title,13,GOLD,true));c.addView(label(value,21,WHITE,true));}
    private EditText input(String hint,int type){EditText e=new EditText(this);e.setSingleLine(true);e.setHint(hint);e.setTextColor(WHITE);e.setHintTextColor(0xff999999);e.setInputType(type);
        e.setBackgroundTintList(android.content.res.ColorStateList.valueOf(GOLD));LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=12;body.addView(e,p);return e;}
    private void button(String s,Runnable r){Button b=new Button(this);b.setText(s);b.setAllCaps(false);b.setTextColor(BG);b.setBackgroundTintList(android.content.res.ColorStateList.valueOf(GOLD));b.setOnClickListener(v->r.run());
        LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=8;body.addView(b,p);}
    private void login(){shell("تسجيل الدخول");body.addView(label("مرحباً بك في حسابك الرقمي",15,WHITE,false));
        EditText id=input("رقم الحساب أو اسم المستخدم",android.text.InputType.TYPE_CLASS_TEXT);
        EditText pw=input("كلمة المرور",android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD);
        button("دخول آمن",()->{JSONObject j=new JSONObject();try{j.put("identity",id.getText().toString().trim());j.put("password",pw.getText().toString());}catch(Exception ignored){}
            call("POST","/auth/login",null,j,result->{String t=result.optString("token","");if(t.isEmpty()){toast(result.optString("message","تعذر تسجيل الدخول"));return;}
                token=t;getSharedPreferences(PREF,MODE_PRIVATE).edit().putString(KEY,t).apply();dashboard();});});}
    private interface Done{void accept(JSONObject j);}
    private void call(String method,String path,String bearer,JSONObject payload,Done done){worker.execute(()->{JSONObject result=new JSONObject();HttpURLConnection c=null;
        try{c=(HttpURLConnection)new URL(API+path).openConnection();c.setRequestMethod(method);c.setConnectTimeout(12000);c.setReadTimeout(15000);c.setRequestProperty("Accept","application/json");
            if(bearer!=null&&!bearer.isEmpty())c.setRequestProperty("Authorization","Bearer "+bearer);
            if(payload!=null){c.setDoOutput(true);c.setRequestProperty("Content-Type","application/json; charset=utf-8");try(OutputStream o=c.getOutputStream()){o.write(payload.toString().getBytes(StandardCharsets.UTF_8));}}
            InputStream in=c.getResponseCode()<400?c.getInputStream():c.getErrorStream();try(BufferedReader r=new BufferedReader(new InputStreamReader(in,StandardCharsets.UTF_8))){StringBuilder s=new StringBuilder();String line;while((line=r.readLine())!=null)s.append(line);result=new JSONObject(s.toString());}
        }catch(Exception ex){try{result.put("message","تعذر الاتصال بالخادم. تحقق من الإنترنت.");}catch(Exception ignored){}}
        finally{if(c!=null)c.disconnect();}JSONObject answer=result;runOnUiThread(()->done.accept(answer));});}
    private void dashboard(){shell("الرئيسية");body.addView(label("جارٍ تحميل بيانات الحساب...",14,WHITE,false));
        call("GET","/dashboard",token,null,j->{if(!j.optBoolean("ok")){logout();toast(j.optString("message","يلزم تسجيل الدخول مجدداً"));return;}
            JSONObject user=j.optJSONObject("user"),bal=j.optJSONObject("balances");shell("الرئيسية");
            if(user!=null)body.addView(label("أهلاً، "+user.optString("full_name",""),17,WHITE,true));
            if(bal!=null){card("رصيد SDG",fmt(bal.optLong("SDG"))+" SDG");card("رصيد URC",fmt(bal.optLong("URC"))+" URC");}
            button("المحفظة",()->section("/wallet","المحفظة"));button("المعاملات السابقة",()->section("/history","سجل المعاملات"));
            JSONArray recent=j.optJSONArray("recent");body.addView(label("آخر العمليات",16,GOLD,true));if(recent!=null)for(int i=0;i<recent.length();i++){JSONObject x=recent.optJSONObject(i);if(x!=null)card(x.optString("memo","عملية")+" · "+x.optString("created_at",""),fmt(x.optLong("delta"))+" "+x.optString("currency",""));}
            button("تحديث",this::dashboard);button("تسجيل الخروج",this::logout);
        });}
    private void section(String path,String title){shell(title);body.addView(label("جارٍ التحميل...",14,WHITE,false));call("GET",path,token,null,j->{
        if(!j.optBoolean("ok")){toast(j.optString("message","تعذر تحميل البيانات"));return;}shell(title);JSONObject b=j.optJSONObject("balances");
        if(b!=null){card("SDG",fmt(b.optLong("SDG"))+" SDG");card("URC",fmt(b.optLong("URC"))+" URC");}
        JSONArray rows=j.optJSONArray("entries");if(rows==null)rows=j.optJSONArray("items");if(rows!=null)for(int i=0;i<rows.length();i++){JSONObject x=rows.optJSONObject(i);if(x==null)continue;
            String value=x.has("delta")?fmt(x.optLong("delta"))+" "+x.optString("currency",""):fmt(x.optLong("amount"))+" "+x.optString("currency","");
            card(x.optString("memo","عملية")+"\n"+x.optString("created_at",""),value);}
        button("الرئيسية",this::dashboard);});}
    private String fmt(long n){return String.format(java.util.Locale.US,"%,d",n);}
    private void logout(){token="";getSharedPreferences(PREF,MODE_PRIVATE).edit().clear().apply();login();}
    private void toast(String s){Toast.makeText(this,s,Toast.LENGTH_LONG).show();}
    @Override public void onBackPressed(){dashboard();}
    @Override protected void onDestroy(){worker.shutdownNow();super.onDestroy();}
}
