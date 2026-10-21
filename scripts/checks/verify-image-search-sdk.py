"""用故障消息重放宿主定位、原图读取和 SDK 解包；默认离线，--live-search 才向搜索引擎上传图片。"""
from pathlib import Path
import argparse
import asyncio
import json
import os
import sys


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/home/qqbot/QQbot/MaiBot'))
    parser.add_argument('--message-id',default='1793846406')
    parser.add_argument('--session-id',default='eea9ec46d0880b86983de2e4235f4bca')
    parser.add_argument('--live-search',action='store_true',help='真实调用插件反查后端，不调用模型或发送群消息')
    args=parser.parse_args()
    os.chdir(args.root);sys.path.insert(0,str(args.root))
    from maibot_sdk.context import PluginContext
    from src.core.tooling import ToolExecutionContext, ToolInvocation
    from src.plugin_runtime.capabilities.data import RuntimeDataCapabilityMixin
    from src.plugin_runtime.component_query import ComponentQueryService
    from src.services import message_service
    from plugins.image_reverse_search.plugin import ImageReverseSearchPlugin

    anchor=message_service.get_message_by_id(args.message_id,args.session_id)
    assert anchor is not None,'故障消息不存在'
    context=ToolExecutionContext(session_id=anchor.session_id,stream_id=anchor.session_id,
        platform=anchor.platform,metadata={'anchor_message':anchor})
    from types import SimpleNamespace
    entry=SimpleNamespace(plugin_id='maibot-team.image-reverse-search',invoke_method='plugin.invoke_tool')
    invocation=ToolInvocation(tool_name='image_reverse_search',arguments={})
    payload=ComponentQueryService._build_tool_invocation_payload(entry,invocation,context)
    host=RuntimeDataCapabilityMixin();reads=[]

    async def rpc(method,plugin_id,data):
        """只允许真实读取消息；保留宿主包装，由 SDK 自行解包。"""
        assert method=='cap.call' and data['capability']=='message.get_by_id'
        reads.append(data['args']['message_id'])
        return await host._cap_message_get_by_id(plugin_id,data['capability'],data['args'])

    class RecordingBackend:
        """记录真正到达后端的图片，隔离外部搜索及其网络、配额。"""
        def __init__(self):self.images=[]
        async def search(self,engine,image,timeout):
            self.images.append((engine,image.fingerprint()))
            return []

    plugin=ImageReverseSearchPlugin()
    plugin.set_plugin_config(plugin.build_default_config())
    plugin._ctx=PluginContext(entry.plugin_id,rpc_call=rpc)
    plugin._rebuild_service()
    backend=RecordingBackend()
    if not args.live_search:plugin._service._backend=backend
    result=await plugin.handle_search_tool(**payload)
    assert result['success'] is True,result
    if not args.live_search:
        assert backend.images,'原图未到达搜索后端'
        assert len({fingerprint for _,fingerprint in backend.images})==1,'引擎收到不同原图'
    print(json.dumps({'sdk_image_path_verified':True,'anchor_message_id':payload.get('anchor_message_id'),
        'reply_to_message_id':payload.get('reply_to_message_id'),'messages_read':reads,
        'backend_images':backend.images,'external_search':args.live_search,'qq_sent':False,
        'search_result':result if args.live_search else None},ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
