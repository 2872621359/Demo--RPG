#  合成小语种需要传输小语种文本、使用小语种发音人vcn、tte=unicode以及修改文本编码方式
#  错误码链接：https://www.xfyun.cn/document/error-code （code返回错误码时必看）
import websocket
import datetime
import hashlib
import base64
import hmac
import json
from urllib.parse import urlencode
import time
import ssl
from wsgiref.handlers import format_date_time
from datetime import datetime
from time import mktime
import _thread as thread
import os
import wave
from pydub import AudioSegment
# import playsound   # 使用 playsound 库播放音频（云端服务器不支持）

STATUS_FIRST_FRAME = 0  # 第一帧的标识
STATUS_CONTINUE_FRAME = 1  # 中间帧标识
STATUS_LAST_FRAME = 2  # 最后一帧的标识

class Ws_Param(object):
    # 初始化
    def __init__(self, APPID, APIKey, APISecret, Text):
        self.APPID = APPID
        self.APIKey = APIKey
        self.APISecret = APISecret
        self.Text = Text

        # 公共参数(common)
        self.CommonArgs = {"app_id": self.APPID}
        # 业务参数(business)，更多个性化参数可在官网查看
        self.BusinessArgs = {"aue": "raw", "auf": "audio/L16;rate=16000", "vcn": "xiaoyan", "tte": "utf8"}
        self.Data = {"status": 2, "text": str(base64.b64encode(self.Text.encode('utf-8')), "UTF8")}
        # 使用小语种须使用以下方式，此处的unicode指的是 utf16小端的编码方式，即"UTF-16LE"”
        # self.Data = {"status": 2, "text": str(base64.b64encode(self.Text.encode('utf-16')), "UTF8")}

    # 生成url
    def create_url(self):
        url = 'wss://tts-api.xfyun.cn/v2/tts'
        # 生成RFC1123格式的时间戳
        now = datetime.now()
        date = format_date_time(mktime(now.timetuple()))

        # 拼接字符串
        signature_origin = "host: " + "ws-api.xfyun.cn" + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + "/v2/tts " + "HTTP/1.1"
        # 进行hmac-sha256进行加密
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'),
                             digestmod=hashlib.sha256).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')

        authorization_origin = "api_key=\"%s\", algorithm=\"%s\", headers=\"%s\", signature=\"%s\"" % (
            self.APIKey, "hmac-sha256", "host date request-line", signature_sha)
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        # 将请求的鉴权参数组合为字典
        v = {
            "authorization": authorization,
            "date": date,
            "host": "ws-api.xfyun.cn"
        }
        # 拼接鉴权参数，生成url
        url = url + '?' + urlencode(v)
        # print("date: ",date)
        # print("v: ",v)
        # 此处打印出建立连接时候的url,参考本demo的时候可取消上方打印的注释，比对相同参数时生成的url与自己代码生成的url是否一致
        # print('websocket url :', url)
        return url


class WebSocketApp(websocket.WebSocketApp):
    def __init__(self, url, ws_param, **kwargs):
        super().__init__(url, **kwargs)
        self.ws_param = ws_param
        self.is_closed = False
        self.audio_complete = False  # 添加音频完成标志
        self.data_received = 0  # 添加数据接收计数
        
    def handle_close(self):
        """处理连接关闭"""
        if not self.audio_complete:
            print("警告: 连接在音频数据完成前关闭")
        self.is_closed = True

def on_message(ws, message):
    
    try:
        message = json.loads(message)
        code = message["code"]
        sid = message["sid"]
        audio = message["data"]["audio"]
        audio = base64.b64decode(audio)
        status = message["data"]["status"]
        # print(message)
        if status == 2:
            print("ws is closed")
            ws.close()
        if code!= 0:
            errMsg = message["message"]
            # print("sid:%s call error:%s code is:%s" % (sid, errMsg, code))
        else:

            with open('./demo.pcm', 'ab') as f:
                f.write(audio)

    except Exception as e:
        print("receive msg,but parse exception:", e)

# 收到websocket错误的处理
def on_error(ws, error):
    print("### error:", error)

# 收到websocket关闭的处理
def on_close(ws, close_status_code, close_msg):
    print("### closed ###")
    # 设置关闭标志
    ws.is_closed = True

# 收到websocket连接建立的处理
def on_open(ws):
    def run(*args):
        d = {"common": wsParam.CommonArgs,
             "business": wsParam.BusinessArgs,
             "data": wsParam.Data,
             }
        d = json.dumps(d)
        print("------>开始发送文本数据")
        ws.send(d)
        if os.path.exists('./demo.pcm'):
            os.remove('./demo.pcm')

    thread.start_new_thread(run, ()) 

# 将生成语音文件的pcm格式转换成wav格式，未指定绝对文件路径，保存在所在文件目录下（左侧）
def pcm2wav(pcm_file, wav_file, channels=1, bits=16, sample_rate=16000):
    # 打开 PCM 文件
    pcmf = open(pcm_file, 'rb')
    pcmdata = pcmf.read()
    pcmf.close()

    # 调用音频播放模块播放生成的wav文件
    # 打开将要写入的 WAVE 文件
    with open(wav_file, 'wb') as wavfile:
    # 设置声道数
        wavfile.setnchannels(channels)
        # 设置采样位宽
        wavfile.setsampwidth(bits // 8)
        # 设置采样率
        wavfile.setframerate(sample_rate)
        # 写入 data 部分
        wavfile.writeframes(pcmdata)
    wavfile.close()

if __name__ == "__main__":
    wsParam = Ws_Param(APPID='a3270512', APISecret='YzMxMDBkM2FkNzIwN2QzYjgxZmJjYjcw',
                     APIKey='8ce9f9ead6de3aca608d8c5fcd1cfbe9',
                     Text="随着科技的飞速发展，教育领域也迎来了前所未有的变革。从传统的课堂教学到线上学习平台，再到虚拟现实和人工智能在教育中的应用，我们正处在一个教育模式不断演变的时代。本文将探讨未来教育的一些趋势，以及技术如何与人文相结合，共同塑造一个更加全面和高效的学习环境。")
    websocket.enableTrace(False)
    wsUrl = wsParam.create_url()
    ws = websocket.WebSocketApp(wsUrl, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.on_open = on_open
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
    pcm2wav("demo.pcm", "demo.wav")
    from pydub import AudioSegment
    song = AudioSegment.from_wav("demo.wav")
    song.export("wav-mp3.mp3", format="mp3")
    # playsound.playsound("demo.wav")  # 云端服务器不支持本地播放
    # 明确导出所有需要的对象供其他模块使用
__all__ = ['Ws_Param', 'WebSocketApp', 'pcm2wav', 'on_error', 'on_close', 'on_message', 'on_open']