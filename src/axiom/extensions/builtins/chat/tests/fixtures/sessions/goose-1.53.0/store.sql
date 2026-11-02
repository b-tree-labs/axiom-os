CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                user_set_name BOOLEAN DEFAULT FALSE,
                session_type TEXT NOT NULL DEFAULT 'user',
                working_dir TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                extension_data TEXT DEFAULT '{}',
                total_tokens INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                accumulated_total_tokens INTEGER,
                accumulated_input_tokens INTEGER,
                accumulated_output_tokens INTEGER,
                accumulated_cache_read_tokens INTEGER,
                accumulated_cache_write_tokens INTEGER,
                accumulated_cost REAL,
                schedule_id TEXT,
                recipe_json TEXT,
                user_recipe_values_json TEXT,
                provider_name TEXT,
                model_config_json TEXT,
                goose_mode TEXT NOT NULL DEFAULT 'auto',
                archived_at TIMESTAMP,
                project_id TEXT,
                parent_session_id TEXT
            );
INSERT INTO sessions VALUES('20261008_1','pong check','',0,'user','/sandbox/work','2026-10-08 19:33:34','2026-10-08 19:34:21','{"enabled_extensions.v0":{"extensions":[{"type":"platform","name":"developer","description":"developer","display_name":null,"bundled":null}]}}',1293,1291,2,NULL,NULL,2548,2504,44,0,0,NULL,NULL,NULL,NULL,'ollama','{"model_name":"qwen2.5:7b","temperature":null,"max_tokens":null,"toolshim":false,"toolshim_model":null}','auto',NULL,NULL,NULL);
CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_timestamp INTEGER NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens INTEGER,
                metadata_json TEXT
            );
INSERT INTO messages VALUES(1,'msg_b95c8c40-dab6-4cab-af33-f9b8c6f289e8','20261008_1','user','[{"type":"text","text":"Use the shell tool to run: echo @@SECRET@@. Then reply with the single word pong."}]',1791488014,'2026-10-08 19:33:34',NULL,'{"userVisible":true,"agentVisible":true}');
INSERT INTO messages VALUES(2,'msg_876ab277-d541-4267-b056-fbe0bb0d4d74','20261008_1','user','[{"type":"text","text":"<turn-context>\n<current-time>2026-10-08 14:33:00 -05:00</current-time>\n<working-directory>/sandbox/work</working-directory>\n</turn-context>"}]',1791488014,'2026-10-08 19:33:34',NULL,'{"userVisible":false,"agentVisible":true,"turnContext":true}');
INSERT INTO messages VALUES(3,'chatcmpl-204','20261008_1','assistant','[{"type":"toolRequest","id":"call_aiyme0k7","toolCall":{"status":"success","value":{"name":"shell","arguments":{"command":"echo @@SECRET@@"}}},"_meta":{"goose.toolCall.providerIndex":0,"goose_extension":"developer"}}]',1791488029,'2026-10-08 19:33:49',NULL,'{"userVisible":true,"agentVisible":true,"inference":{"provider":"ollama","requestedModel":"qwen2.5:7b"},"usage":{"inputTokens":1213,"outputTokens":42,"totalTokens":1255,"elapsedMs":15206,"timeToFirstTokenMs":15148}}');
INSERT INTO messages VALUES(4,'msg_8938d01f-9acd-4126-afd7-ecba4fc4ab1b','20261008_1','user','[{"type":"toolResponse","id":"call_aiyme0k7","toolResult":{"status":"success","value":{"resultType":"complete","content":[{"type":"text","text":"@@SECRET@@","annotations":{"priority":0.0}}],"structuredContent":{"stdout":"@@SECRET@@","stderr":"","exit_code":0},"isError":false}}}]',1791488029,'2026-10-08 19:33:49',NULL,'{"userVisible":true,"agentVisible":true}');
INSERT INTO messages VALUES(5,'chatcmpl-883','20261008_1','assistant','[{"type":"text","text":"pong"}]',1791488061,'2026-10-08 19:34:21',NULL,'{"userVisible":true,"agentVisible":true,"inference":{"provider":"ollama","requestedModel":"qwen2.5:7b"},"usage":{"inputTokens":1291,"outputTokens":2,"totalTokens":1293,"elapsedMs":32090,"timeToFirstTokenMs":32075}}');
CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
INSERT INTO schema_version VALUES(16,'2026-10-08 19:33:34');
