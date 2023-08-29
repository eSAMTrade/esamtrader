import json
import uuid
import logging
from typing import Any, Dict, List, Optional, Union
from kafka import KafkaConsumer, TopicPartition
from retry import retry


logger = logging.getLogger(__name__)


class KafkaMsgConsumer:
    def __init__(self, topics: List[str], address: str, port: int = 9092, group_id: Optional[str] = None):
        if group_id is None:
            group_id = uuid.uuid4().hex
        self._kafka_consumer = KafkaConsumer(bootstrap_servers=f"{address}:{port}", group_id=group_id)
        for topic in topics:
            partitions = self._kafka_consumer.partitions_for_topic(topic)
            tps = [TopicPartition(topic, p) for p in partitions]
            self._kafka_consumer.assign(tps)
        self.topics = topics

    @retry(exceptions=Exception, delay=0.1, max_delay=64, logger=logger)
    def read_msg(self) -> Union[Dict[str, Any], List[Any]]:
        # next is waiting until next message arrives
        msg = next(self._kafka_consumer)
        value = json.loads(msg.value.decode("utf-8"))
        return value