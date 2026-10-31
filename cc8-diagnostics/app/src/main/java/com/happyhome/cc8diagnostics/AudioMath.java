package com.happyhome.cc8diagnostics;

/** Platform-independent PCM metrics; never interprets noise as a pass/fail verdict. */
public final class AudioMath {
    public long samples, zeroSamples, clippedSamples;
    public double squares;
    public int peak;
    public void add(short[] pcm, int offset, int length) {
        if (offset < 0 || length < 0 || offset > pcm.length - length) throw new IllegalArgumentException("Invalid PCM range");
        for (int i=offset;i<offset+length;i++) {
            int value=pcm[i];
            int absolute=Math.abs(value);
            squares+=(double)value*value;
            samples++;
            if (value==0) zeroSamples++;
            if (absolute>=32760) clippedSamples++;
            peak=Math.max(peak,absolute);
        }
    }
    public double rms() { return samples==0 ? 0 : Math.sqrt(squares/samples); }
    public double dbfs() { return rms()==0 ? -120 : Math.max(-120,20*Math.log10(rms()/32768.0)); }
    public double zeroRatio() { return samples==0 ? 0 : (double)zeroSamples/samples; }
    public double clipRatio() { return samples==0 ? 0 : (double)clippedSamples/samples; }
    public double peakDbfs() { return peak==0 ? -120 : 20*Math.log10(peak/32768.0); }
    public static short[] repeat(short[] source,int frames) {
        if (source.length==0 || frames<0) throw new IllegalArgumentException("Empty reference or invalid duration");
        short[] result=new short[frames];
        for (int i=0;i<frames;i++) result[i]=source[i%source.length];
        return result;
    }
}
