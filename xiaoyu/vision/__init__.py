"""视觉层（S6 才做，先占个位置）。

计划：
    读字  -> RapidOCR
    认人  -> InsightFace
    扫码  -> OpenCV 的 QRCodeDetector（不用 pyzbar，省掉装 DLL 的麻烦）

原则：意图命中"看看 / 这是谁 / 写的什么"才开摄像头，不要一直开着。
"""