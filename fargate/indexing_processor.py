import json
from chunking.fixedsize_chunking import FixedSizeChunker
from chunking.hierarical_chunking import HieraricalChunker
from embedding.titanv2_embedding import TitanV2Embedding
from embedding.titanv1_embedding import TitanV1Embedding
from fargate.base_task_processor import BaseFargateTaskProcessor
from logger.global_logger import get_logger
from reader.pdf_reader import PDFReader
from storage.db.dynamodb import DynamoDB
from storage.db.vector import OpenSearchClient
from storage.s3_storage import S3StorageProvider
from indexing.indexing import Index
from config.config import Config
from config.env_config_provider import EnvConfigProvider
from storage.storage_provider_factory import StorageProviderFactory
from embedding.embedding_registry import embedding_registry
from chunking.chunking_provider_factory import ChunkingFactory

from embedding.bge_large_embedding import BGELargeEmbedding, BGEM3Embedding, GTEQwen2Embedding


logger = get_logger()
env_config_provider = EnvConfigProvider()
config = Config(env_config_provider)

class IndexingProcessor(BaseFargateTaskProcessor):
    """
    Processor for indexing tasks in Fargate.
    """

    def process(self):
        logger.info("Starting indexing process.")
        try:
            exp_config_data = self.input_data

            # exp_config_data = {
            #     "kb_data": "file://C:/Projects/refactor/medical_abstracts_100_169kb.pdf",
            #     "chunk_size": 256,
            #     "chunk_overlap": 5,
            #     "parent_chunk_size": 512,
            #     "embedding_model": "huggingface-sentencesimilarity-bge-large-en-v1-5",
            #     "aws_region": "us-east-1",
            #     "chunking_strategy": "Fixed",
            #     "experiment_id": "CK016M3Z",
            #     "execution_id": "S2ROS",
            #     "vector_dimension": 1024,
            #     "embedding_service": "sagemaker",    # sagemaker/bedrock
            # }


            logger.info("Into indexing processor. Processing event: %s", json.dumps(exp_config_data))

            index_id = exp_config_data.get("index_id") # "local-index-1024"
            
            kb_data = exp_config_data.get("kb_data")
            storage = StorageProviderFactory.create_storage_provider(kb_data)
            kb_data_path = storage.get_path(kb_data)
            pdf_reader = PDFReader(storage)

            chunking = ChunkingFactory.create_chunker(
                exp_config_data.get("chunking_strategy"), 
                exp_config_data.get("chunk_size"), 
                exp_config_data.get("chunk_overlap"),
                exp_config_data.get("parent_chunk_size", None)
            )

            embedding_class = embedding_registry.get_model(exp_config_data.get("embedding_model"))
            embedding = embedding_class(exp_config_data.get("embedding_model"), exp_config_data.get("aws_region"), exp_config_data.get("vector_dimension"))


            indexing = Index(pdf_reader, chunking, embedding)
            embeddings_list = indexing.index(kb_data_path)


            # adding total_index_embed_tokens to dynamo db table: experiment_table
            experiment_db = DynamoDB(config.get_experiment_table_name(), config.get_region()) 
            experiment_db.update(
                key={'id': exp_config_data.get("experiment_id")},
                data={'index_embed_tokens': embeddings_list.metadata.input_tokens }
            )


            open_search_client = OpenSearchClient(
                config.get_opensearch_host(), 
                config.get_opensearch_port(),
                config.get_opensearch_username(), 
                config.get_opensearch_password(),
                index_id
            )
    
            bulk_data = []
            for embedding in embeddings_list.embeddings:
                # TODO See this index also can be included in the to_dict method
                bulk_data.append({"index": {"_index": index_id}})
                bulk_data.append(embedding.to_json())
            open_search_client.write_bulk(body=bulk_data)
            output = {"status": "success", "message": "Indexing completed successfully."}
            self.send_task_success(output)
        except Exception as e:
            logger.error(f"Error during indexing process: {str(e)}")
            self.send_task_failure(str(e))
