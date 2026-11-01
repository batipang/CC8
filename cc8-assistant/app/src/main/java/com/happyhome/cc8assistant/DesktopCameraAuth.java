package com.happyhome.cc8assistant;

/** Connection-bound, one-use authorization from the already paired Mac. */
final class DesktopCameraAuth {
    // 每条连接重置 challenge，结合 nonce 和递增 generation 拒绝旧连接的拍照重放。
    private String challenge;private int lastGeneration;
    DesktopCameraAuth(){reset();}
    synchronized void reset(){byte[] random=new byte[16];new java.security.SecureRandom().nextBytes(random);challenge=TransportAuth.hex(random);lastGeneration=-1;}
    synchronized String challenge(){return challenge;}
    synchronized boolean accept(String token,String nonce,int generation,String signature){
        if(token==null||!token.matches("[a-f0-9]{64}")||nonce==null||!nonce.matches("[a-f0-9]{32}")||generation<=lastGeneration)return false;
        try{String expected=TransportAuth.sign(token,"desktop-camera:"+challenge+":"+nonce+":"+generation);if(!TransportAuth.matches(expected,signature))return false;}
        catch(java.security.GeneralSecurityException|IllegalArgumentException error){return false;}
        lastGeneration=generation;return true;
    }
}
