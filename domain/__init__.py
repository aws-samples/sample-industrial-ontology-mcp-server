"""도메인 계층 패키지.

이 패키지를 처음 import 할 때 프로세스 전체 SPARQL egress 차단점을 설치한다
(``domain.sparql_templates.install_sparql_egress_chokepoint``). 서버는 도구를 등록할 때
도구 모듈을 import 하고, 그 import 가 ``domain.*`` 을 함께 import 하므로 첫 질의 전에
설치가 끝난다.
"""
from domain.sparql_templates import install_sparql_egress_chokepoint

install_sparql_egress_chokepoint()
