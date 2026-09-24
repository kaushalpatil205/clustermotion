## 2. Architecture Diagram

```mermaid
flowchart TD
    Client((Client)) --> ALB[AWS Application Load Balancer]
    
    subgraph AWS Cloud
        ALB -->|Traffic Shift| Blue[Blue EKS Cluster 1.34]
        ALB -->|Traffic Shift| Green[Green EKS Cluster 1.36]
        
        subgraph Blue [Blue EKS Cluster]
            B_API(Stateless APIs)
            B_Worker(Queue Consumers)
            B_DB[(PostgreSQL Primary)]
        end
        
        subgraph Green [Green EKS Cluster]
            G_API(Stateless APIs)
            G_Worker(Queue Consumers)
            G_DB[(PostgreSQL Replica)]
        end
        
        B_API --> B_DB
        G_API --> G_DB
        B_DB -.->|WAL Streaming via S3| G_DB
        
        B_Worker -.->|Lease Locked| DynamoDB[(DynamoDB Lease)]
        G_Worker -.->|Lease Wait| DynamoDB
    end
```
