import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

public enum WindowImage {
    public static func size(width: Double, height: Double) throws -> (Int, Int) {
        guard width.isFinite, height.isFinite, width >= 1, height >= 1,
              width <= 100000, height <= 100000 else { throw WindowInputError.invalidImage }
        let ratio = min(1, 1280 / max(width, height), sqrt(Double(1280 * 960) / (width * height)))
        return (max(1,Int(floor(width * ratio))), max(1,Int(floor(height * ratio))))
    }

    /// Encodes only the supplied CGImage. This method cannot capture a screen or write a file.
    public static func encode(_ image: CGImage) throws -> (Data, Int, Int) {
        guard image.width > 0, image.height > 0,
              image.width <= 16384, image.height <= 16384,
              image.width * image.height <= 4096 * 4096 else { throw WindowInputError.imageTooLarge }
        let (width,height) = try size(width: Double(image.width),height: Double(image.height))
        guard let color = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(data:nil,width:width,height:height,bitsPerComponent:8,
                                      bytesPerRow:width*4,space:color,
                                      bitmapInfo:CGImageAlphaInfo.premultipliedLast.rawValue)
        else { throw WindowInputError.invalidImage }
        context.setFillColor(CGColor(red:0,green:0,blue:0,alpha:1))
        context.fill(CGRect(x:0,y:0,width:width,height:height))
        context.interpolationQuality = .high
        context.draw(image,in:CGRect(x:0,y:0,width:width,height:height))
        guard let bounded = context.makeImage() else { throw WindowInputError.invalidImage }
        for quality in [0.75,0.60,0.45,0.30] {
            let output = NSMutableData()
            guard let destination = CGImageDestinationCreateWithData(output,UTType.jpeg.identifier as CFString,1,nil)
            else { throw WindowInputError.invalidImage }
            CGImageDestinationAddImage(destination,bounded,
                [kCGImageDestinationLossyCompressionQuality:quality] as CFDictionary)
            guard CGImageDestinationFinalize(destination) else { throw WindowInputError.invalidImage }
            if output.length > 0 && output.length <= 768*1024 { return (output as Data,width,height) }
        }
        throw WindowInputError.imageTooLarge
    }
}
