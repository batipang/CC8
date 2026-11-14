"""Compile actual source presentation against minimal JSON containers, without Android I/O."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]


class NativeSourcesTests(unittest.TestCase):
    def test_real_bounds_immutable_metadata_labels_and_safe_links(self):
        source = (
            ROOT
            / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/SourcesPresentation.java'
        )
        obj = """package org.json;import java.util.*;public class JSONObject {
 public final Map<String,Object> data=new HashMap<>();
 public JSONObject(Object...pairs){for(int i=0;i<pairs.length;i+=2)data.put((String)pairs[i],pairs[i+1]);}
 public Object opt(String key){return data.get(key);}}"""
        array = """package org.json;public class JSONArray{
 final Object[] rows;public JSONArray(Object...rows){this.rows=rows;}
 public int length(){return rows.length;}
 public JSONObject optJSONObject(int i){return rows[i] instanceof JSONObject?(JSONObject)rows[i]:null;}}"""
        harness = """package com.happyhome.cc8assistant;import org.json.*;
public class SourceCheck {
 static void yes(boolean value,String why){if(!value)throw new AssertionError(why);}
 public static void main(String[] args){
 String url="https://example.com/2026/11/01/news";
 JSONObject row=new JSONObject("title","原始来源","url",url,"access_evidence","answer_link_only","body_verified",true);
 SourcesPresentation s=new SourcesPresentation(new JSONArray(row),"2026-11-09T20:00:00+08:00","answer_completed",null,4,9);
 yes(s.size()==1&&s.route==4&&s.generation==9,"source owner lost");
 yes(s.at(0).label().contains("读取状态未知")&&s.at(0).label().contains("来源标注日期：未知；事件日期未知"),"URL or model flag invented evidence/dates");
 yes(s.heading().startsWith("回答来源汇总时间"),"answer time became retrieval time");
 yes(!s.details(0).contains("查询参考的北京时间"),"legacy envelope invented a query time");
 for(Object badTime:new Object[]{true,"2026-02-30T23:59:59+08:00","2026-11-08T23:59:59+00:00","2026-11-08T24:00:00+08:00"})
  yes(!new SourcesPresentation(new JSONArray(row),"now","answer_completed",badTime,4,9).details(0).contains("查询参考的北京时间"),"invalid date became the question reference");
 row.data.put("url","https://other.example/changed");row.data.put("title","new");
 yes(s.at(0).url.equals(url)&&s.at(0).title.equals("原始来源"),"caller mutated display receipt");
 JSONObject page=new JSONObject("title","页面","url",url,"access_evidence","page_action_completed","published_at","来源称 3 天前");
 s=new SourcesPresentation(new JSONArray(page),"now","search_response",null,4,9);
 yes(s.at(0).label().contains("正文未核实")&&s.at(0).label().contains("来源标注日期：来源称 3 天前"),"operation or relative date promoted");
 yes(s.heading().startsWith("检索返回时间"),"actual search timestamp lost basis");
 JSONObject read=new JSONObject("title","已读取页面","url",url,"access_evidence","page_body_received",
  "observed_at","2026-11-10T20:00:00+08:00","page_bytes",577,"page_excerpt","实际页面文本",
  "page_dates","2026-11-10；2026-11-09","page_modified_dates","2026-11-12","final_url","https://other.example/final");
 s=new SourcesPresentation(new JSONArray(read),"now","answer_completed",null,4,9);
 yes(s.at(0).summary().contains("HTTP 页面已读取，事实仍需核对"),"HTTP receipt became fact verification or was hidden");
 yes(s.at(0).label().contains("实际页面文本")&&s.at(0).label().contains("事件日期未知")&&s.at(0).label().contains("https://other.example/final"),"real text, unknown event date or redirect receipt lost");
 yes(s.at(0).label().contains("页面发布日期标注：2026-11-10；2026-11-09")&&s.at(0).label().contains("页面更新日期标注：2026-11-12"),"publication and modification were conflated");
 yes(s.at(0).label().contains("页面读取结束的北京时间 2026-11-10 20:00:00"),"actual read completion time was hidden");
 read.data.put("page_excerpt","mutated");read.data.put("page_dates","mutated");read.data.put("page_modified_dates","mutated");
 yes(!s.at(0).label().contains("mutated"),"mutable page data rewrote the source view");
 read.data.put("page_bytes",true);
 yes(!new SourcesPresentation(new JSONArray(read),"","",null,4,9).at(0).summary().contains("HTTP 页面已读取"),"malformed page receipt promoted");
 read.data.put("page_bytes",577);read.data.put("page_details_limited",true);
 yes(new SourcesPresentation(new JSONArray(read),"","",null,4,9).at(0).label().contains("受传输大小限制"),"omitted details were concealed");
 JSONObject coverage=new JSONObject("title","范围","url",url,"access_evidence","page_body_received",
  "observed_at","now","page_bytes",1000,"page_excerpt","汉😀字","page_text_chars",20000,"page_text_truncated",true);
 SourcesPresentation.Source scoped=new SourcesPresentation(new JSONArray(coverage),"now","answer_completed",null,4,9).at(0);
 yes(scoped.pageRead&&scoped.label().contains("页面读取结束的北京时间 未知"),"invalid read timestamp erased or invented evidence");
 yes(scoped.label().contains("保留文字：20000 字符；当前摘录：3 字符")&&scoped.label().contains("后文未核对"),"UTF16 units or truncation promoted full-page coverage");
 coverage.data.put("page_text_chars",3);coverage.data.put("page_text_truncated",false);
 yes(scoped.label().contains("20000 字符")&&scoped.label().contains("后文未核对"),"mutable coverage rewrote the receipt");
 scoped=new SourcesPresentation(new JSONArray(coverage),"","",null,4,9).at(0);
 yes(scoped.label().contains("读取器未因文字上限截断")&&!scoped.label().contains("后文未核对"),"known complete retained text hidden");
 for(Object bad:new Object[]{null,"false",0}){coverage.data.put("page_text_truncated",bad);
  yes(new SourcesPresentation(new JSONArray(coverage),"","",null,4,9).at(0).label().contains("截断状态：未提供"),"missing/coerced boolean became completeness proof");}
 coverage.data.put("page_text_chars",2);coverage.data.put("page_text_truncated",false);
 yes(new SourcesPresentation(new JSONArray(coverage),"","",null,4,9).at(0).label().contains("文字数量：未提供"),"inconsistent excerpt count trusted");
 coverage.data.put("page_bytes",1.0);
 yes(!new SourcesPresentation(new JSONArray(coverage),"","",null,4,9).at(0).pageRead,"floating-point bytes manufactured body evidence");
 coverage.data.put("page_bytes",1000);coverage.data.put("page_text_chars",20000);
 coverage.data.remove("page_excerpt");coverage.data.put("page_text_truncated",true);coverage.data.put("page_details_limited",true);
 String omitted=new SourcesPresentation(new JSONArray(coverage),"","",null,4,9).at(0).label();
 yes(omitted.contains("当前摘录：0 字符")&&omitted.contains("受传输大小限制")&&omitted.contains("后文未核对"),"omitted transport data counted as displayed excerpt");
 JSONObject failure=new JSONObject("title","失败来源","url",url,"access_evidence","page_fetch_failed",
  "page_error_code","unsupported_encoding","page_http_status",200,"page_excerpt","未取得正文",
  "final_url","https://other.example/final");
 String failed=new SourcesPresentation(new JSONArray(failure),"","",null,4,9).at(0).label();
 yes(failed.contains("UTF-8")&&failed.contains("HTTP 响应：200；未取得可用正文")&&!failed.contains("正文摘录"),"HTTP 200 or stale excerpt manufactured body evidence");
 yes(failed.contains("最后尝试的地址：https://other.example/final"),"terminal failure destination hidden");
 yes(failed.contains("页面读取结束的北京时间 未知"),"missing failed-read timestamp became known");
 failure.data.put("answer_cited",false);
 SourcesPresentation.Source uncited=new SourcesPresentation(new JSONArray(failure),"","",null,4,9).at(0);
 yes(uncited.summary().contains("回答未引用")&&uncited.label().contains("回答未引用"),"uncited read not distinguished");
 failure.data.put("answer_cited","false");
 yes(!new SourcesPresentation(new JSONArray(failure),"","",null,4,9).at(0).label().contains("回答未引用"),"malformed citation status considered known");
 failure.data.put("final_url","http://127.0.0.1/private");
 failure.data.put("page_error_code","/private/credential-secret");failure.data.put("page_http_status",true);
 failed=new SourcesPresentation(new JSONArray(failure),"","",null,4,9).at(0).label();
 yes(!failed.contains("127.0.0.1")&&!failed.contains("credential-secret")&&failed.contains("未提供可核对的原因")&&failed.contains("未取得可核对状态"),"untrusted reason or malformed status displayed");
 String emoji=new String(Character.toChars(0x1f600));
 JSONObject unicode=new JSONObject("title",emoji.repeat(160),"url",url);
 s=new SourcesPresentation(new JSONArray(unicode),"","",null,4,9);
 yes(s.size()==1&&s.at(0).title.codePointCount(0,s.at(0).title.length())==160,"host-valid non-BMP title was discarded");
 unicode.data.put("title",emoji.repeat(161));yes(new SourcesPresentation(new JSONArray(unicode),"","",null,4,9).size()==0,"scalar title limit widened");
 String longUrl="https://example.com/"+emoji.repeat(2048-"https://example.com/".length());
 yes(SourcesPresentation.safeUrl(longUrl)&&!SourcesPresentation.safeUrl(longUrl+emoji),"URL limits used UTF16 units or exceeded host scalar bound");
 unicode.data.put("title",emoji.repeat(160));unicode.data.put("url",longUrl);
 s=new SourcesPresentation(new JSONArray(unicode),"","",null,4,9);yes(s.size()==1&&s.at(0).url.equals(longUrl),"valid long Unicode URL lost at field parse");
 for(String malformed:new String[]{Character.toString((char)0xd800),Character.toString((char)0xdc00),"x"+(char)0xd800+"y"}){
  unicode.data.put("title",malformed);yes(new SourcesPresentation(new JSONArray(unicode),"","",null,4,9).size()==0,"broken title surrogate accepted");
  yes(!SourcesPresentation.safeUrl(url+malformed),"broken URL surrogate accepted");
 }
 JSONObject large=new JSONObject("title","内容","url",url,"access_evidence","page_body_received","observed_at","now","page_bytes",8000,
  "page_excerpt",emoji.repeat(2000),"page_dates",String.join("；",java.util.Collections.nCopies(8,emoji.repeat(64))));
 scoped=new SourcesPresentation(new JSONArray(large),"","",null,4,9).at(0);
 yes(scoped.pageExcerpt.codePointCount(0,scoped.pageExcerpt.length())==2000&&scoped.pageDates.codePointCount(0,scoped.pageDates.length())==519,"excerpt/date scalar limits disagree with host");
 large.data.put("page_excerpt",emoji.repeat(2001));yes(new SourcesPresentation(new JSONArray(large),"","",null,4,9).at(0).pageExcerpt.isEmpty(),"excerpt scalar budget exceeded");
 for(String bad:new String[]{"javascript:alert(1)","http://localhost/a","https://user:secret@example.com/a","https://127.0.0.1/a","https://0177.0.0.1/a","https://127.1/a","https://0x7f.0.0.1/a","https://[::ffff:127.0.0.1]/a","https://10.0.0.1/a","https://169.254.1.1/a","https://machine.local/a","https://machine.local./a","https://example.com:22/a","https://example.com/a b"})
  yes(!SourcesPresentation.safeUrl(bad),"unsafe URL accepted: "+bad);
 Object[] many=new Object[20];java.util.Arrays.fill(many,page);
 yes(new SourcesPresentation(new JSONArray(many),"","",null,4,9).size()==6,"unbounded sources");
 row=new JSONObject("title",true,"url",url);yes(new SourcesPresentation(new JSONArray(row),"","",null,4,9).size()==0,"coerced title accepted");
 row=new JSONObject("title","source","url",url,"access_evidence","made_up_verification");
 yes(new SourcesPresentation(new JSONArray(row),"","",null,4,9).at(0).label().contains("读取状态未知"),"unknown evidence trusted");
 }
}"""
        java = java_bin()
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            files = []
            for name, contents in [
                ('JSONObject.java', obj),
                ('JSONArray.java', array),
                ('SourceCheck.java', harness),
            ]:
                path = folder / name
                path.write_text(contents)
                files.append(str(path))
            result = subprocess.run(
                [str(java / 'javac'), '-encoding', 'UTF-8', '-d', str(folder), str(source), *files],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [str(java / 'java'), '-cp', str(folder), 'com.happyhome.cc8assistant.SourceCheck'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
