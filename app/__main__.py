TITLE = '区域经营拐点复核台'
OPERATION = 'regional_series'
BREAK_OPERATION = 'common_break_scan'

import argparse
import json

from app.httpd import create_server
from app.service import Service


def main():
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument('--source', default='data/source.json')
    parser.add_argument('--database', default='var/analyses.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('inspect', help='保存业务覆盖分析')
    run.add_argument('--as-of', default='2026-09-30T23:59:59Z')
    run.add_argument('--request-id', default='coverage-september')
    breaks = commands.add_parser('breaks', help='共同转折与地区转折复核')
    breaks.add_argument('--as-of', default='2026-09-30T23:59:59Z')
    breaks.add_argument('--request-id', default='common-break-scan-demo')
    breaks.add_argument('--hypothesized-month', default=None,
                        help='待复核的假定转折月，YYYY-MM')
    breaks.add_argument('--resamples', type=int, default=None,
                        help='自助法重抽样次数，默认 199')
    server = commands.add_parser('serve', help='启动本地分析接口')
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8080)
    commands.add_parser('demo', help='展示当前覆盖分析')
    args = parser.parse_args()
    service = Service(args.source, args.database)
    if args.command == 'serve':
        instance = create_server(service, args.host, args.port)
        print(json.dumps({'地址': instance.server_address}, ensure_ascii=False), flush=True)
        try:
            instance.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            instance.server_close()
    elif args.command == 'breaks':
        options = {}
        if args.hypothesized_month:
            options['hypothesized_month'] = args.hypothesized_month
        if args.resamples is not None:
            options['resamples'] = args.resamples
        request = {'operation': BREAK_OPERATION, 'request_id': args.request_id,
                   'as_of': args.as_of, 'options': options}
        print(json.dumps(service.run(request), ensure_ascii=False, indent=2))
    else:
        request = {'operation': OPERATION, 'request_id': getattr(args, 'request_id', 'demo-current'),
                   'as_of': getattr(args, 'as_of', '2026-09-30T23:59:59Z')}
        print(json.dumps(service.run(request), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
