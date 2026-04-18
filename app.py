from flask import Flask, request, jsonify, render_template, send_from_directory
from typing_extensions import TypedDict
from typing import Annotated, Callable, List, Tuple, Union
from langgraph.graph.message import add_messages
from langgraph.graph import MessagesState, START, END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langchain_core.messages import AnyMessage, RemoveMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from dotenv import load_dotenv
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
import threading

#import torch
import os
import re
import json
import random
import sys
import inspect
import time
import requests

from keda import Ws_Param, pcm2wav, on_error, on_close, WebSocketApp
import websocket
import ssl
import _thread as thread
import base64
import wave

import warnings
from urllib3.exceptions import InsecureRequestWarning

# 禁用未验证的HTTPS请求警告
warnings.simplefilter('ignore', InsecureRequestWarning)

currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.insert(0, parentdir)

app = Flask(__name__)

# 定义音频文件保存路径（使用绝对路径避免工作目录问题）
PCM_OUTPUT_PATH = os.path.join(currentdir, "static", "audio", "output.pcm")
WAV_OUTPUT_PATH = os.path.join(currentdir, "static", "audio", "output.wav")

@app.route('/static/audio/<path:filename>')
def serve_audio(filename):
    return send_from_directory('static/audio', filename)

@app.route('/css/<path:filename>')
def serve_css(filename):
    css_files = {
        "1230characterselection-try.css": "1230characterselection-try.css",
        "chatpage-1227-try1.css": "chatpage-1227-try1.css",
        "1223homepage-try.css": "1223homepage-try.css",
        "1223storycontext-try.css": "1223storycontext-try.css"
    }
    return send_from_directory('css', css_files.get(filename, 'chatpage-1227-try1.css'))

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory('images', filename)

@app.route('/')
def index():
    return render_template('1223homepage-try.html')

@app.route('/storycontextpage')
def storycontextpage():
    return render_template('1223storycontext-try.html')

@app.route('/characterselection')
def characterselection():
    return render_template('1230characterselection-try.html')

@app.route('/chatpage')
def chatpage():
    card = request.args.get('card')
    if card == '1':
        content = {
            "image_path": "../images/characterimage1.png",
            "description": "This is card 1"
        }
        return render_template('chatpage-1227-try1.html', card=card, content=content)
    elif card == '2':
        content = {
            "image_path": "../images/characterimage2.png",
            "description": "This is card 2"
        }
        return render_template('chatpage-1227-try1.html', card=card, content=content)
    elif card == '3':
        content = {
            "image_path": "../images/characterimage3.png",
            "description": "This is card 3"
        }
        return render_template('chatpage-1227-try1.html', card=card, content=content)
    else:
        return render_template('chatpage-1227-try1.html')

class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]

from pydantic import BaseModel

class SayGoodbye(BaseModel):
    """Call if user wants to end this conversation"""
    farewell: str

def get_message_formatted(message: AnyMessage):
    message_formatted = {'role': message.type, 'content': message.content}
    return message_formatted

def load_documents_by_level(document_path):
    """加载文档并按关卡分割，每个chunk携带level元数据"""
    folder_path = document_path
    full_text = ""
    for filename in os.listdir(folder_path):
        if filename.startswith('.') or filename.startswith('~'):
            continue
        file_path = os.path.join(folder_path, filename)
        _, ext = os.path.splitext(filename)
        if ext == ".pdf":
            docs = PyPDFLoader(file_path).load()
        elif ext == ".docx":
            docs = Docx2txtLoader(file_path).load()
        else:
            continue
        full_text += "\n".join(d.page_content for d in docs) + "\n"

    level_pattern = re.compile(r'(节点\d+[：:][^\n]*)', re.MULTILINE)
    parts = level_pattern.split(full_text)

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800, chunk_overlap=80, length_function=len, is_separator_regex=False
    )

    all_chunks = []

    # level 0：节点1之前的背景/开场内容
    if parts[0].strip():
        intro_chunks = text_splitter.create_documents(
            [parts[0].strip()],
            metadatas=[{"level": 0, "level_title": "背景信息"}]
        )
        all_chunks.extend(intro_chunks)

    # level 1~N：每个节点
    i = 1
    while i < len(parts) - 1:
        header = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        m = re.match(r'节点(\d+)[：:](.*)', header)
        level_num = int(m.group(1)) if m else (i + 1) // 2
        level_title = m.group(2).strip() if m else header.strip()

        node_chunks = text_splitter.create_documents(
            [f"{header}\n{content}".strip()],
            metadatas=[{"level": level_num, "level_title": level_title}]
        )
        all_chunks.extend(node_chunks)
        i += 2

    # 为每个chunk生成唯一ID：level:title:index
    level_counter = {}
    for chunk in all_chunks:
        key = chunk.metadata["level"]
        idx = level_counter.get(key, 0)
        chunk.metadata["id"] = f"level{key}:{idx}"
        level_counter[key] = idx + 1

    return all_chunks

class ChatBot:

    def __init__(self, model: ChatGoogleGenerativeAI, embeddings: GoogleGenerativeAIEmbeddings, system_prompt: str, document_path: str, chroma_path: str, num_messages=5, thread_id: str = "1") -> None:
        self.system_prompt = system_prompt
        self.num_messages = num_messages
        self.model = model
        self.config = {"configurable": {"thread_id": thread_id}}
        self.chroma_path = chroma_path
        
        self.message_history = []
        self.db = Chroma(
            persist_directory=chroma_path,
            embedding_function=embeddings
        )
        chunks = load_documents_by_level(document_path)
        self.add_to_chroma(chunks)
        self.app = self.get_app()

        self.current_node = 0   # 0=背景, 1~7=各节点
        self.max_node = 7

        self.user_attributes = {
            "力量": 78,
            "敏捷": 65,
            "幸运": 66,
            "智力": 80
        }
        


    def add_to_chroma(self, chunks: list[Document]):
        existing_items = self.db.get(include=[])
        existing_ids = set(existing_items.get("ids", [])) if existing_items else set()

        new_chunks = [chunk for chunk in chunks if chunk.metadata["id"] not in existing_ids]
        if new_chunks:
            new_chunk_ids = [chunk.metadata["id"] for chunk in new_chunks]
            self.db.add_documents(new_chunks, ids=new_chunk_ids)
    
    def get_app(self):
        def should_continue(state):
            messages = state["messages"]
            last_message = messages[-1]
            if not last_message.tool_calls:
                return "ask_human"
                return END
            else:
                return "agent"

        def call_model(state):
            messages = state["messages"]
            human_message = messages[-1]
            # 先检索当前节点内容，再检索背景层(level=0)作为补充
            results = self.db.similarity_search(
                human_message.content, k=3,
                filter={"level": self.current_node}
            )
            if not results:
                results = self.db.similarity_search(human_message.content, k=3)
            context = "\n".join([doc.page_content for doc in results])
            node_hint = f"节点{self.current_node}" if self.current_node > 0 else "背景信息"
            messages.append({"role": "system", "content": (
                f"【当前剧情进度：{node_hint}】\n"
                f"请严格按照当前节点的剧情引导玩家，不要跳到后续节点。\n"
                f"当玩家完成本节点的核心目标后，在回复末尾加上[ADVANCE_NODE]。\n"
                f"相关剧本内容：{context}"
            )})
            response = self.model.invoke(messages)
            message_formatted = get_message_formatted(response)
            self.message_history.append(message_formatted)
            message_list = []
            if len(messages) > self.num_messages + 1:
                remove_count = len(messages) - self.num_messages - 1
                message_list = [RemoveMessage(id=m.id) for m in messages[1:1+remove_count]]
            message_list.append(response)
            return {"messages": message_list}
        
        def ask_human(state):
            user_message = interrupt("What can I help you?")
            message = {'role': 'user', 'content': user_message}
            self.message_history.append(message)
            return {"messages": [message]}

        workflow = StateGraph(MessagesState)

        workflow.add_node("agent", call_model)
        workflow.add_node("ask_human", ask_human)

        workflow.add_edge(START, "agent")
        workflow.add_conditional_edges(
            "agent",
            should_continue,
        )
        workflow.add_edge("ask_human", "agent")
        memory = MemorySaver()

        chatbot = workflow.compile(checkpointer=memory)
        return chatbot
    
    def stream(self, message: str):
        response_text = ""
        state = self.app.get_state(self.config)
        if not state.values or len(state.values.get("messages", [])) == 0:
            self.message_history.append({'role': 'user', 'content': message})
            for event in self.app.stream({'messages': [('system', self.system_prompt), ('user', message)]}, self.config, stream_mode="values"):
                response_text = event["messages"][-1].content
                event["messages"][-1].pretty_print()
        else:
            for event in self.app.stream(Command(resume=message), self.config, stream_mode="values"):
                response_text = event["messages"][-1].content
                event["messages"][-1].pretty_print()

        # 检测节点推进信号
        if "[ADVANCE_NODE]" in response_text:
            response_text = response_text.replace("[ADVANCE_NODE]", "").strip()
            if self.current_node < self.max_node:
                self.current_node += 1
                print(f"[节点推进] 当前节点：{self.current_node}")

        return response_text

def on_open(ws):
    def run(*args):
        if not hasattr(ws, 'ws_param'):
            print("wsParam未初始化")
            return

        d = {"common": ws.ws_param.CommonArgs,
             "business": ws.ws_param.BusinessArgs,
             "data": ws.ws_param.Data,
             }
        d = json.dumps(d)
        print("------>开始发送文本数据")
        ws.send(d)
        if os.path.exists(PCM_OUTPUT_PATH):
            os.remove(PCM_OUTPUT_PATH)

    thread.start_new_thread(run, ())

def on_message(ws, message):
    try:
        message = json.loads(message)
        code = message["code"]
        sid = message["sid"]
        audio = message["data"]["audio"]
        audio = base64.b64decode(audio)
        status = message["data"]["status"]
        print(message)
        if status == 2:
            print("ws is closed")
            ws.close()
        if code != 0:
            errMsg = message["message"]
            print("sid:%s call error:%s code is:%s" % (sid, errMsg, code))
        else:
            with open(PCM_OUTPUT_PATH, 'ab') as f:
                f.write(audio)
    except Exception as e:
        print("receive msg, but parse exception:", e)

def pcm2wav(pcm_file, wav_file, channels=1, bits=16, sample_rate=16000):
    # 确保目录存在
    os.makedirs(os.path.dirname(wav_file), exist_ok=True)

    # 打开 PCM 文件
    with open(pcm_file, 'rb') as pcmf:
        pcmdata = pcmf.read()

    # 打开将要写入的 WAVE 文件
    with wave.open(wav_file, 'wb') as wavfile:
        # 设置声道数
        wavfile.setnchannels(channels)
        # 设置采样位宽
        wavfile.setsampwidth(bits // 8)
        # 设置采样率
        wavfile.setframerate(sample_rate)
        # 写入 data 部分
        wavfile.writeframes(pcmdata)
    return True

def generate_unique_filename(base_path, extension):
    unique_filename = f"{base_path}_{int(time.time())}.{extension}"
    return unique_filename

def my_send_message(chatbot: ChatBot):
    message = request.form.get('message')
    print("收到的消息:", message)
    if message:
        try:
            response = chatbot.stream(message=message)
            response_text = response if isinstance(response, str) else str(response)
            print("AI响应:", response_text)
            
            if response_text.strip():
                os.makedirs(os.path.dirname(PCM_OUTPUT_PATH), exist_ok=True)

                if "生成关于力量的随机数" in response_text:
                    attribute = "力量"
                elif "生成关于敏捷的随机数" in response_text:
                    attribute = "敏捷"
                elif "生成关于幸运的随机数" in response_text:
                    attribute = "幸运"
                elif "生成关于智力的随机数" in response_text:
                    attribute = "智力"
                else:
                    attribute = None
                
                if attribute:
                    random_number = random.randint(1, 100)
                    attribute_value = chatbot.user_attributes[attribute]
                    print(f"生成的随机数: {random_number}, 属性值: {attribute_value}")
                    
                    if random_number > attribute_value:
                        response_text = "您并未通过鉴定，请去别的地方探索吧"
                    else:
                        response_text = "恭喜你通过了鉴定，请说说下一步您将去哪里探索"
                
                wsParam = Ws_Param(APPID='a3270512', APISecret='YzMxMDBkM2FkNzIwN2QzYjgxZmJjYjcw', APIKey='8ce9f9ead6de3aca608d8c5fcd1cfbe9', Text=response_text)
                websocket.enableTrace(False)
                wsUrl = wsParam.create_url()
                ws = WebSocketApp(wsUrl, 
                                ws_param=wsParam, 
                                on_message=on_message, 
                                on_error=on_error, 
                                on_close=on_close)
                ws.on_open = on_open
                ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
                
                # 生成唯一的 WAV 文件名
                unique_wav_path = generate_unique_filename(WAV_OUTPUT_PATH.rsplit('.', 1)[0], 'wav')
                
                # 删除旧的 WAV 文件
                if os.path.exists(unique_wav_path):
                    os.remove(unique_wav_path)
                
                if not pcm2wav(PCM_OUTPUT_PATH, unique_wav_path):
                    print("PCM转WAV失败")
                    return jsonify({
                        "status": "error",
                        "message": "Failed to convert audio"
                    })
                
                if os.path.exists(unique_wav_path):
                    file_size = os.path.getsize(unique_wav_path)
                    print(f"WAV文件大小: {file_size} bytes")
                    
                    if file_size <= 44:
                        print("生成的WAV文件无效：文件太小")
                        return jsonify({
                            "status": "error",
                            "message": "Invalid WAV file generated"
                        })
                        
                    print(f"文件权限: {oct(os.stat(unique_wav_path).st_mode)[-3:]}")
                    
                    return jsonify({
                        "status": "success",
                        "answer": response_text,
                        "audio_url": unique_wav_path
                    })
                else:
                    print("WAV文件未生成")
                    return jsonify({
                        "status": "error",
                        "message": "WAV file not generated"
                    })
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Error in my_send_message: {str(e)}")
            return jsonify({
                "status": "error",
                "message": str(e)
            })
    return jsonify({"status": "error", "message": "No message provided"})

def my_get_messages(chatbot: ChatBot):
    chat_history = chatbot.message_history
    return jsonify({"chat_history": chat_history})

def my_gamestart(chatbot: ChatBot):
    try:
        response_text = (
            "欢迎各位调查员来到这个充满神秘与悬疑的故事中。"
            "在密歇根州的阿诺兹堡市，有这样一栋小屋，坐落在艾尔斯伯里大街218号。"
            "这栋小屋曾经是道格拉斯·金博尔的住所。道格拉斯是一个离群索居的中年人，书籍是他生命中最重要的东西。"
            "他常常在家中的书房、教室，甚至是附近的墓地读书。"
            "他会坐在一处特定的低矮墓碑上阅读几个小时，在一个满月的夜晚，他左侧的一块石板滑动开来，"
            "一只食尸鬼从中探出头来，从此他们建立了奇妙的友谊。"
            "五年后，道格拉斯走进了食尸鬼的世界并定居下来。"
            "一年后，他的生活习惯和样貌都发生了变化，比如饮食口味变得特殊，而且更喜欢晚上读书。"
            "后来，他闯入自己的旧居取回心爱的书籍，而如今的旧居主人是他的侄子托马斯·金博尔。"
            "现在，托马斯联系上了你们这些调查员，因为他的叔叔道格拉斯失踪已经一年了，没有留下任何踪迹。"
            "而在最近，托马斯发现叔叔珍藏的五本书不翼而飞。"
            "这些书虽然在旁人看来并不值钱，但对于道格拉斯来说无比珍贵。"
            "托马斯希望你们能找到偷窃书籍的人，尽可能追回书籍，并且调查他的叔叔是否还活着。"
            "那么各位调查员，你们要怎么开始这个任务呢？"
            "是先去调查道格拉斯的旧居，还是探索墓地寻找道格拉斯的踪迹呢？"
        )
        chatbot.message_history.append({'role': 'ai', 'content': response_text})
        chatbot.current_node = 1  # 游戏正式开始，进入节点1：接任务

        # 把开场白追加到 system_prompt，让 AI 后续对话时知道自己说过什么
        chatbot.system_prompt += f"\n\n你已经对玩家说了以下开场白：\n{response_text}\n请基于这个开场白继续引导玩家。"

        os.makedirs(os.path.dirname(PCM_OUTPUT_PATH), exist_ok=True)

        wsParam = Ws_Param(APPID='a3270512', APISecret='YzMxMDBkM2FkNzIwN2QzYjgxZmJjYjcw', APIKey='8ce9f9ead6de3aca608d8c5fcd1cfbe9', Text=response_text)
        websocket.enableTrace(False)
        wsUrl = wsParam.create_url()
        ws = WebSocketApp(wsUrl,
                          ws_param=wsParam,
                          on_message=on_message,
                          on_error=on_error,
                          on_close=on_close)
        ws.on_open = on_open
        ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

        unique_wav_path = generate_unique_filename(WAV_OUTPUT_PATH.rsplit('.', 1)[0], 'wav')
        if os.path.exists(unique_wav_path):
            os.remove(unique_wav_path)

        if not pcm2wav(PCM_OUTPUT_PATH, unique_wav_path):
            return jsonify({"status": "error", "message": "Failed to convert audio"})

        if os.path.exists(unique_wav_path) and os.path.getsize(unique_wav_path) > 44:
            return jsonify({
                "status": "success",
                "message": response_text,
                "audio_url": unique_wav_path
            })
        else:
            return jsonify({"status": "success", "message": response_text})

    except Exception as e:
        print(f"Error in my_gamestart: {str(e)}")
        return jsonify({"status": "error", "message": str(e)})

def initialize_gemini_embeddings(api_key):
    return GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=api_key
    )

DOC_PATH = os.path.join(currentdir, "document")
CHROMA_PATH = os.path.join(currentdir, "chroma")

# 加载 api_keys.json（本地开发用），Render 上通过环境变量配置
_api_keys_path = os.path.join(currentdir, 'api_keys.json')
if os.path.exists(_api_keys_path):
    with open(_api_keys_path) as f:
        content = json.load(f)
        for k, v in content.items():
            os.environ[k] = v

gemini_api_key = os.getenv("GEMINI_API_KEY")

model = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=gemini_api_key,
)

embeddings = initialize_gemini_embeddings(api_key=gemini_api_key)

system_prompt = (
    "你是一名跑团游戏的DM，负责引导玩家在跑团游戏中探索故事和解决谜题；你的任务是：\n"
    "1. 在开始时主动提供故事背景；\n"
    "2. 回答玩家的问题，提供必要的线索。\n"
    "3. 引导玩家逐步接触谜题，例如：\n"
    "   - 调查道格拉斯的旧居。\n"
    "   - 探索坟地，寻找道格拉斯的踪迹。\n"
    "   - 发现食尸鬼的秘密。\n"
    "4. 根据玩家的行动，动态调整故事的发展。\n"
    "5. 保持神秘感和悬疑感，逐步揭示故事的真相。"
)

chatbot = ChatBot(model=model, embeddings=embeddings, system_prompt=system_prompt,
                  document_path=DOC_PATH, chroma_path=CHROMA_PATH, thread_id="1")

@app.route('/gamestart', methods=['GET'])
def gamestart():
    return my_gamestart(chatbot=chatbot)

@app.route('/send_message', methods=['POST'])
def send_message():
    return my_send_message(chatbot=chatbot)

@app.route('/get_messages', methods=['GET'])
def get_messages():
    return my_get_messages(chatbot=chatbot)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5003))
    app.run(port=port, debug=False, use_reloader=False)

