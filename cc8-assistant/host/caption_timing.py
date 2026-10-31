"""Merge cumulative MiniMax word snapshots; send only bounded timing numbers."""
import math


class CaptionTiming:
    def __init__(self,text):
        self.text=text;self.segments={};self.publish=None
        self.offsets=[0]
        for char in text:self.offsets.append(self.offsets[-1]+len(char.encode('utf-16-le'))//2)

    def feed(self,subtitle):
        if not isinstance(subtitle,dict):return
        try:
            begin=int(subtitle['text_begin']);end=int(subtitle['text_end'])
            finish=float(subtitle['time_end']);words=subtitle['timestamped_words']
            if not 0<=begin<=end<=len(self.text) or not math.isfinite(finish) or not 0<=finish<=90000 or not isinstance(words,list) or len(words)>2048:return
            rows=[]
            for word in words:
                a=int(word['word_begin']);b=int(word['word_end']);start=float(word['time_begin']);stop=float(word['time_end'])
                if not begin<=a<b<=end or not all(math.isfinite(v) for v in (start,stop)) or not 0<=start<=stop<=90000:return
                rows.append([self.offsets[a],self.offsets[b],round(start*16),round(stop*16)])
            previous=self.segments.get(begin)
            # Final summary contains older snapshots too; never regress.
            if previous and (finish,len(rows))<=(previous[0],len(previous[1])):return
            self.segments[begin]=(finish,rows)
            if self.publish:self.publish(self.rows())
        except (ValueError,TypeError,KeyError,OverflowError):return

    def rows(self):
        rows={}
        for _,values in sorted(self.segments.values(),key=lambda v:v[0]):
            for row in values:rows[(row[0],row[1])]=row
        result=sorted(rows.values(),key=lambda r:(r[0],r[2]))
        return result[:2048]
