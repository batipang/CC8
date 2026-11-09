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
 SourcesPresentation s=new SourcesPresentation(new JSONArray(row),"2026-11-09T20:00:00+08:00","answer_completed",4,9);
 yes(s.size()==1&&s.route==4&&s.generation==9,"source owner lost");
 yes(s.at(0).label().contains("读取状态未知")&&s.at(0).label().contains("来源标注日期：未知；事件日期未知"),"URL or model flag invented evidence/dates");
 yes(s.heading().startsWith("回答来源汇总时间"),"answer time became retrieval time");
 row.data.put("url","https://other.example/changed");row.data.put("title","new");
 yes(s.at(0).url.equals(url)&&s.at(0).title.equals("原始来源"),"caller mutated display receipt");
 JSONObject page=new JSONObject("title","页面","url",url,"access_evidence","page_action_completed","published_at","来源称 3 天前");
 s=new SourcesPresentation(new JSONArray(page),"now","search_response",4,9);
 yes(s.at(0).label().contains("正文未核实")&&s.at(0).label().contains("来源标注日期：来源称 3 天前"),"operation or relative date promoted");
 yes(s.heading().startsWith("检索返回时间"),"actual search timestamp lost basis");
 for(String bad:new String[]{"javascript:alert(1)","http://localhost/a","https://user:secret@example.com/a","https://127.0.0.1/a","https://0177.0.0.1/a","https://127.1/a","https://0x7f.0.0.1/a","https://[::ffff:127.0.0.1]/a","https://10.0.0.1/a","https://169.254.1.1/a","https://machine.local/a","https://machine.local./a","https://example.com:22/a","https://example.com/a b"})
  yes(!SourcesPresentation.safeUrl(bad),"unsafe URL accepted: "+bad);
 Object[] many=new Object[20];java.util.Arrays.fill(many,page);
 yes(new SourcesPresentation(new JSONArray(many),"","",4,9).size()==6,"unbounded sources");
 row=new JSONObject("title",true,"url",url);yes(new SourcesPresentation(new JSONArray(row),"","",4,9).size()==0,"coerced title accepted");
 row=new JSONObject("title","source","url",url,"access_evidence","made_up_verification");
 yes(new SourcesPresentation(new JSONArray(row),"","",4,9).at(0).label().contains("读取状态未知"),"unknown evidence trusted");
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
