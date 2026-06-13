@echo off

docker run -d ^
  --name fhir-neo4j ^
  -p 7474:7474 ^
  -p 7687:7687 ^
  -e NEO4J_AUTH=neo4j/password ^
  -e "NEO4J_PLUGINS=[\"apoc\"]" ^
  -e NEO4J_dbms_security_procedures_unrestricted=apoc.* ^
  neo4j:latest

pause
